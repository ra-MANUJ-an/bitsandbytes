from typing import Iterable, Literal, Optional, Tuple

import torch

import bitsandbytes.functional as F
from bitsandbytes.optim.optimizer import Optimizer2State


class _ReferenceAdEMAMix(torch.optim.Optimizer):
    """
    Reference: https://hf.co/papers/2409.03137
    """

    def __init__(
        self,
        params: Iterable[torch.nn.Parameter],
        lr: float = 1e-3,
        betas: Tuple[float, float, float] = (0.9, 0.999, 0.9999),
        alpha: float = 5.0,
        eps: float = 1e-8,
        weight_decay: float = 1e-2,  # default 0.0 or 1e-2?
        t_beta3: Optional[int] = None,
        t_alpha: Optional[int] = None,
    ):
        defaults = dict(
            lr=lr, betas=betas, alpha=alpha, eps=eps, weight_decay=weight_decay, t_beta3=t_beta3, t_alpha=t_alpha
        )

        super().__init__(params, defaults)

    @torch.no_grad()
    def step(self, closure=None):
        loss = None

        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            if "step" in group:
                group["step"] += 1
            else:
                group["step"] = 1

            lr = group["lr"]
            eps = group["eps"]
            beta1, beta2, beta3_final = group["betas"]
            alpha_final = group["alpha"]
            weight_decay = group["weight_decay"]

            for p in group["params"]:
                if p.grad is None:
                    continue

                grad = p.grad
                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    # For parity with bnb implementation we combine both fast
                    # and slow EMA stats into one stacked tensor.
                    state["m1_m2"] = p.new_zeros((2, *p.size()))
                    state["nu"] = torch.zeros_like(p)  # second moment estimate

                m1, m2, nu = state["m1_m2"][0], state["m1_m2"][1], state["nu"]

                bias_correction1 = 1 - beta1 ** group["step"]

                bias_correction2 = 1 - beta2 ** group["step"]

                # TODO: Implement schedulers for alpha/beta3
                beta3 = beta3_final
                alpha = alpha_final

                # Update the EMAs
                m1.mul_(beta1).add_(grad, alpha=1 - beta1)
                m2.mul_(beta3).add_(grad, alpha=1 - beta3)
                nu.mul_(beta2).addcmul_(grad, grad, value=1 - beta2)

                # Compute step
                denom = (nu.sqrt() / (bias_correction2**0.5)).add(eps)
                update = (m1.div(bias_correction1) + alpha * m2) / denom

                # Add weight decay
                update.add_(p, alpha=weight_decay)

                # Apply update scaled by learning rate
                p.add_(-lr * update)

        return loss


class AdEMAMix(Optimizer2State):
    def __init__(
        self,
        params: Iterable[torch.nn.Parameter],
        lr: float = 1e-3,
        betas: Tuple[float, float, float] = (0.9, 0.999, 0.9999),
        alpha: float = 5.0,
        t_alpha: Optional[int] = None,
        t_beta3: Optional[int] = None,
        eps: float = 1e-8,
        weight_decay: float = 1e-2,
        optim_bits: Literal[8, 32] = 32,
        min_8bit_size: int = 4096,
        is_paged: bool = False,
    ):
        super().__init__(
            "ademamix",
            params=params,
            lr=lr,
            betas=betas,
            eps=eps,
            weight_decay=weight_decay,
            optim_bits=optim_bits,
            args=None,
            min_8bit_size=min_8bit_size,
            percentile_clipping=100,
            block_wise=True,
            is_paged=is_paged,
            alpha=alpha,
        )

    @torch.no_grad()
    def init_state(self, group, p, gindex, pindex):
        # In our AdEMAMix implementation, we use `state` to hold
        # both the fast and slow EMAs. Here we override the base
        # `Optimizer2State` to allocate a buffer twice as large.
        # Additional consideration: we do not support block_wise=False,
        # percentile clipping, or max_unorm.

        config = self.get_config(gindex, pindex, group)

        if config["optim_bits"] == 32:
            dtype = torch.float32
        elif config["optim_bits"] == 8:
            dtype = torch.uint8
        else:
            raise NotImplementedError(f'Amount of optimizer bits not supported: {config["optim_bits"]}')

        if p.numel() < config["min_8bit_size"]:
            dtype = torch.float32

        state = self.state[p]
        state["step"] = 0

        if dtype == torch.uint8:
            if "dynamic" not in self.name2qmap:
                self.fill_qmap()
            self.name2qmap["dynamic"] = state["qmap1"] = self.name2qmap["dynamic"].to(p.device)
            self.name2qmap["udynamic"] = state["qmap2"] = self.name2qmap["udynamic"].to(p.device)

            n = p.numel()
            blocks = (n // 2048) + bool(n % 2048)

            state["absmax1"] = torch.zeros((2, blocks), dtype=torch.float32, device=p.device)
            state["absmax2"] = torch.zeros((blocks,), dtype=torch.float32, device=p.device)

        state["state1"] = self._get_state_double_buffer(p, dtype=dtype)
        state["state2"] = self.get_state_buffer(p, dtype=dtype)

    def _get_state_double_buffer(self, p, dtype=torch.float32):
        if not self.is_paged or p.numel() < 0.5e5:
            return torch.zeros((2, *p.size()), dtype=dtype, device=p.device)
        else:
            buff = F.get_paged(*(2, *p.size()), dtype=dtype, device=p.device)
            F.fill(buff, 0)
            self.page_mng.paged_tensors.append(buff)
            return buff


class AdEMAMix8bit(AdEMAMix):
    def __init__(
        self,
        params: Iterable[torch.nn.Parameter],
        lr: float = 1e-3,
        betas: Tuple[float, float, float] = (0.9, 0.999, 0.9999),
        alpha: float = 5.0,
        t_alpha: Optional[int] = None,
        t_beta3: Optional[int] = None,
        eps: float = 1e-8,
        weight_decay: float = 1e-2,
        min_8bit_size: int = 4096,
        is_paged: bool = False,
    ):
        super().__init__(
            params,
            lr=lr,
            betas=betas,
            alpha=alpha,
            t_alpha=t_alpha,
            t_beta3=t_beta3,
            eps=eps,
            weight_decay=weight_decay,
            optim_bits=8,
            min_8bit_size=min_8bit_size,
            is_paged=is_paged,
        )


class PagedAdEMAMix8bit(AdEMAMix8bit):
    def __init__(
        self,
        params: Iterable[torch.nn.Parameter],
        lr: float = 1e-3,
        betas: Tuple[float, float, float] = (0.9, 0.999, 0.9999),
        alpha: float = 5.0,
        t_alpha: Optional[int] = None,
        t_beta3: Optional[int] = None,
        eps: float = 1e-8,
        weight_decay: float = 1e-2,
        min_8bit_size: int = 4096,
    ):
        super().__init__(
            params,
            lr=lr,
            betas=betas,
            alpha=alpha,
            t_alpha=t_alpha,
            t_beta3=t_beta3,
            eps=eps,
            weight_decay=weight_decay,
            min_8bit_size=min_8bit_size,
            is_paged=True,
        )


class PagedAdEMAMix(AdEMAMix):
    def __init__(
        self,
        params: Iterable[torch.nn.Parameter],
        lr: float = 1e-3,
        betas: Tuple[float, float, float] = (0.9, 0.999, 0.9999),
        alpha: float = 5.0,
        t_alpha: Optional[int] = None,
        t_beta3: Optional[int] = None,
        eps: float = 1e-8,
        weight_decay: float = 1e-2,
        optim_bits: Literal[8, 32] = 32,
        min_8bit_size: int = 4096,
    ):
        super().__init__(
            params,
            lr=lr,
            betas=betas,
            alpha=alpha,
            t_alpha=t_alpha,
            t_beta3=t_beta3,
            eps=eps,
            weight_decay=weight_decay,
            optim_bits=optim_bits,
            min_8bit_size=min_8bit_size,
            is_paged=True,
        )


class AdEMAMix32bit(Optimizer2State):
    def __init__(
        self,
        params: Iterable[torch.nn.Parameter],
        lr: float = 1e-3,
        betas: Tuple[float, float, float] = (0.9, 0.999, 0.9999),
        alpha: float = 5.0,
        t_alpha: Optional[int] = None,
        t_beta3: Optional[int] = None,
        eps: float = 1e-8,
        weight_decay: float = 1e-2,
        min_8bit_size: int = 4096,
        is_paged: bool = False,
    ):
        super().__init__(
            "ademamix",
            params=params,
            lr=lr,
            betas=betas,
            eps=eps,
            weight_decay=weight_decay,
            optim_bits=32,
            args=None,
            min_8bit_size=min_8bit_size,
            percentile_clipping=100,
            block_wise=True,
            is_paged=is_paged,
            alpha=alpha,
        )


class PagedAdEMAMix32bit(AdEMAMix32bit):
    def __init__(
        self,
        params: Iterable[torch.nn.Parameter],
        lr: float = 1e-3,
        betas: Tuple[float, float, float] = (0.9, 0.999, 0.9999),
        alpha: float = 5.0,
        t_alpha: Optional[int] = None,
        t_beta3: Optional[int] = None,
        eps: float = 1e-8,
        weight_decay: float = 1e-2,
        min_8bit_size: int = 4096,
    ):
        super().__init__(
            params,
            lr=lr,
            betas=betas,
            alpha=alpha,
            t_alpha=t_alpha,
            t_beta3=t_beta3,
            eps=eps,
            weight_decay=weight_decay,
            min_8bit_size=min_8bit_size,
            is_paged=True,
        )