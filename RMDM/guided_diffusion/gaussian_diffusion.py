"""
This code started out as a PyTorch port of Ho et al's diffusion models:
https://github.com/hojonathanho/diffusion/blob/1e0dceb3b3495bbe19116a5e1b3596cd0706c543/diffusion_tf/diffusion_utils_2.py
Docstrings have been added, as well as DDIM sampling and a new collection of beta schedules.
"""
from torch.autograd import Variable
import enum
import torch.nn.functional as F
from torchvision.utils import save_image
import torch
import math
import os
# from visdom import Visdom
# viz = Visdom(port=8850)
import numpy as np
import torch as th
import torch.nn as nn
from .train_util import visualize
from .nn import mean_flat
from .losses import normal_kl, discretized_gaussian_log_likelihood
from scipy import ndimage
from torchvision import transforms
from .utils import staple, dice_score, norm
import torchvision.utils as vutils
from .dpm_solver import NoiseScheduleVP, model_wrapper, DPM_Solver
import string
import random

def standardize(img):
    mean = th.mean(img)
    std = th.std(img)
    img = (img - mean) / std
    return img


def get_named_beta_schedule(schedule_name, num_diffusion_timesteps):
    """
    Get a pre-defined beta schedule for the given name.
    The beta schedule library consists of beta schedules which remain similar
    in the limit of num_diffusion_timesteps.
    Beta schedules may be added, but should not be removed or changed once
    they are committed to maintain backwards compatibility.
    """
    if schedule_name == "linear":
        # Linear schedule from Ho et al, extended to work for any number of
        # diffusion steps.
        scale = 1000 / num_diffusion_timesteps
        beta_start = scale * 0.0001
        beta_end = scale * 0.02
        return np.linspace(
            beta_start, beta_end, num_diffusion_timesteps, dtype=np.float64
        )
    elif schedule_name == "cosine":
        return betas_for_alpha_bar(
            num_diffusion_timesteps,
            lambda t: math.cos((t + 0.008) / 1.008 * math.pi / 2) ** 2,
        )
    else:
        raise NotImplementedError(f"unknown beta schedule: {schedule_name}")


def betas_for_alpha_bar(num_diffusion_timesteps, alpha_bar, max_beta=0.999):
    """
    Create a beta schedule that discretizes the given alpha_t_bar function,
    which defines the cumulative product of (1-beta) over time from t = [0,1].
    :param num_diffusion_timesteps: the number of betas to produce.
    :param alpha_bar: a lambda that takes an argument t from 0 to 1 and
                      produces the cumulative product of (1-beta) up to that
                      part of the diffusion process.
    :param max_beta: the maximum beta to use; use values lower than 1 to
                     prevent singularities.
    """
    betas = []
    for i in range(num_diffusion_timesteps):
        t1 = i / num_diffusion_timesteps
        t2 = (i + 1) / num_diffusion_timesteps
        betas.append(min(1 - alpha_bar(t2) / alpha_bar(t1), max_beta))
    return np.array(betas)


class ModelMeanType(enum.Enum):
    """
    Which type of output the model predicts.
    """

    PREVIOUS_X = enum.auto()  # the model predicts x_{t-1}
    START_X = enum.auto()  # the model predicts x_0
    EPSILON = enum.auto()  # the model predicts epsilon


class ModelVarType(enum.Enum):
    """
    What is used as the model's output variance.
    The LEARNED_RANGE option has been added to allow the model to predict
    values between FIXED_SMALL and FIXED_LARGE, making its job easier.
    """

    LEARNED = enum.auto()
    FIXED_SMALL = enum.auto()
    FIXED_LARGE = enum.auto()
    LEARNED_RANGE = enum.auto()


class LossType(enum.Enum):
    MSE = enum.auto()  # use raw MSE loss (and KL when learning variances)
    RESCALED_MSE = (
        enum.auto()
    )  # use raw MSE loss (with RESCALED_KL when learning variances)
    KL = enum.auto()  # use the variational lower-bound
    RESCALED_KL = enum.auto()  # like KL, but rescale to estimate the full VLB
    BCE_DICE = enum.auto()

    def is_vb(self):
        return self == LossType.KL or self == LossType.RESCALED_KL


class GaussianDiffusion:
    """
    Utilities for training and sampling diffusion models.
    Ported directly from here, and then adapted over time to further experimentation.
    https://github.com/hojonathanho/diffusion/blob/1e0dceb3b3495bbe19116a5e1b3596cd0706c543/diffusion_tf/diffusion_utils_2.py#L42
    :param betas: a 1-D numpy array of betas for each diffusion timestep,
                  starting at T and going to 1.
    :param model_mean_type: a ModelMeanType determining what the model outputs.
    :param model_var_type: a ModelVarType determining how variance is output.
    :param loss_type: a LossType determining the loss function to use.
    :param rescale_timesteps: if True, pass floating point timesteps into the
                              model so that they are always scaled like in the
                              original paper (0 to 1000).
    """

    def __init__(
        self,
        *,
        betas,
        model_mean_type,
        model_var_type,
        loss_type,
        dpm_solver,
        rescale_timesteps=False,
    ):
        self.model_mean_type = model_mean_type
        self.model_var_type = model_var_type
        self.loss_type = loss_type
        self.rescale_timesteps = rescale_timesteps
        self.dpm_solver = dpm_solver

        # Use float64 for accuracy.
        betas = np.array(betas, dtype=np.float64)
        self.betas = betas
        assert len(betas.shape) == 1, "betas must be 1-D"
        assert (betas > 0).all() and (betas <= 1).all()

        self.num_timesteps = int(betas.shape[0])

        alphas = 1.0 - betas
        self.alphas_cumprod = np.cumprod(alphas, axis=0)
        self.alphas_cumprod_prev = np.append(1.0, self.alphas_cumprod[:-1])
        self.alphas_cumprod_next = np.append(self.alphas_cumprod[1:], 0.0)
        assert self.alphas_cumprod_prev.shape == (self.num_timesteps,)

        # calculations for diffusion q(x_t | x_{t-1}) and others
        self.sqrt_alphas_cumprod = np.sqrt(self.alphas_cumprod)
        self.sqrt_one_minus_alphas_cumprod = np.sqrt(1.0 - self.alphas_cumprod)
        self.log_one_minus_alphas_cumprod = np.log(1.0 - self.alphas_cumprod)
        self.sqrt_recip_alphas_cumprod = np.sqrt(1.0 / self.alphas_cumprod)
        self.sqrt_recipm1_alphas_cumprod = np.sqrt(1.0 / self.alphas_cumprod - 1)

        # calculations for posterior q(x_{t-1} | x_t, x_0)
        self.posterior_variance = (
            betas * (1.0 - self.alphas_cumprod_prev) / (1.0 - self.alphas_cumprod)
        )
        # log calculation clipped because the posterior variance is 0 at the
        # beginning of the diffusion chain.
        self.posterior_log_variance_clipped = np.log(
            np.append(self.posterior_variance[1], self.posterior_variance[1:])
        )
        self.posterior_mean_coef1 = (
            betas * np.sqrt(self.alphas_cumprod_prev) / (1.0 - self.alphas_cumprod)
        )
        self.posterior_mean_coef2 = (
            (1.0 - self.alphas_cumprod_prev)
            * np.sqrt(alphas)
            / (1.0 - self.alphas_cumprod)
        )

    def q_mean_variance(self, x_start, t):
        """
        Get the distribution q(x_t | x_0).
        :param x_start: the [N x C x ...] tensor of noiseless inputs.
        :param t: the number of diffusion steps (minus 1). Here, 0 means one step.
        :return: A tuple (mean, variance, log_variance), all of x_start's shape.
        """
        mean = (
            _extract_into_tensor(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start
        )
        variance = _extract_into_tensor(1.0 - self.alphas_cumprod, t, x_start.shape)
        log_variance = _extract_into_tensor(
            self.log_one_minus_alphas_cumprod, t, x_start.shape
        )
        return mean, variance, log_variance

    def q_sample(self, x_start, t, noise=None):
        """
        Diffuse the data for a given number of diffusion steps.
        In other words, sample from q(x_t | x_0).
        :param x_start: the initial data batch.
        :param t: the number of diffusion steps (minus 1). Here, 0 means one step.
        :param noise: if specified, the split-out normal noise.
        :return: A noisy version of x_start.
        """
        if noise is None:
            noise = th.randn_like(x_start)
        assert noise.shape == x_start.shape
        return (
                _extract_into_tensor(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start
                + _extract_into_tensor(self.sqrt_one_minus_alphas_cumprod, t, x_start.shape)
                * noise
        )

    def q_posterior_mean_variance(self, x_start, x_t, t):
        """
        Compute the mean and variance of the diffusion posterior:
            q(x_{t-1} | x_t, x_0)
        """
        assert x_start.shape == x_t.shape
        posterior_mean = (
            _extract_into_tensor(self.posterior_mean_coef1, t, x_t.shape) * x_start
            + _extract_into_tensor(self.posterior_mean_coef2, t, x_t.shape) * x_t
        )
        posterior_variance = _extract_into_tensor(self.posterior_variance, t, x_t.shape)
        posterior_log_variance_clipped = _extract_into_tensor(
            self.posterior_log_variance_clipped, t, x_t.shape
        )
        assert (
            posterior_mean.shape[0]
            == posterior_variance.shape[0]
            == posterior_log_variance_clipped.shape[0]
            == x_start.shape[0]
        )
        return posterior_mean, posterior_variance, posterior_log_variance_clipped


    # def p_mean_variance(
    #     self, model, x, t, clip_denoised=True, denoised_fn=None, model_kwargs=None
    # ):
    #     """
    #     Apply the model to get p(x_{t-1} | x_t), as well as a prediction of
    #     the initial x, x_0.
    #     :param model: the model, which takes a signal and a batch of timesteps
    #                   as input.
    #     :param x: the [N x C x ...] tensor at time t.
    #     :param t: a 1-D Tensor of timesteps.
    #     :param clip_denoised: if True, clip the denoised signal into [-1, 1].
    #     :param denoised_fn: if not None, a function which applies to the
    #         x_start prediction before it is used to sample. Applies before
    #         clip_denoised.
    #     :param model_kwargs: if not None, a dict of extra keyword arguments to
    #         pass to the model. This can be used for conditioning.
    #     :return: a dict with the following keys:
    #              - 'mean': the model mean output.
    #              - 'variance': the model variance output.
    #              - 'log_variance': the log of 'variance'.
    #              - 'pred_xstart': the prediction for x_0.
    #     """
    #     if model_kwargs is None:
    #         model_kwargs = {}
    #     B, C = x.shape[:2]
    #     C=1
    #     cal = 0
    #     assert t.shape == (B,)
    #     model_output = model(x, self._scale_timesteps(t), **model_kwargs)
    #     if isinstance(model_output, tuple):
    #         model_output, cal = model_output
    #     x=x[:,-1:,...]  #loss is only calculated on the last channel, not on the input brain MR image
    #     if self.model_var_type in [ModelVarType.LEARNED, ModelVarType.LEARNED_RANGE]:
    #         assert model_output.shape == (B, C * 2, *x.shape[2:])
    #         model_output, model_var_values = th.split(model_output, C, dim=1)
    #         if self.model_var_type == ModelVarType.LEARNED:
    #             model_log_variance = model_var_values
    #             model_variance = th.exp(model_log_variance)
    #         else:
    #             min_log = _extract_into_tensor(
    #                 self.posterior_log_variance_clipped, t, x.shape
    #             )
    #             max_log = _extract_into_tensor(np.log(self.betas), t, x.shape)
    #             # The model_var_values is [-1, 1] for [min_var, max_var].
    #             frac = (model_var_values + 1) / 2
    #             model_log_variance = frac * max_log + (1 - frac) * min_log
    #             model_variance = th.exp(model_log_variance)
    #     else:
    #         model_variance, model_log_variance = {
    #             # for fixedlarge, we set the initial (log-)variance like so
    #             # to get a better decoder log likelihood.
    #             ModelVarType.FIXED_LARGE: (
    #                 np.append(self.posterior_variance[1], self.betas[1:]),
    #                 np.log(np.append(self.posterior_variance[1], self.betas[1:])),
    #             ),
    #             ModelVarType.FIXED_SMALL: (
    #                 self.posterior_variance,
    #                 self.posterior_log_variance_clipped,
    #             ),
    #         }[self.model_var_type]
    #         model_variance = _extract_into_tensor(model_variance, t, x.shape)
    #         model_log_variance = _extract_into_tensor(model_log_variance, t, x.shape)

    #     def process_xstart(x):
    #         if denoised_fn is not None:
    #             x = denoised_fn(x)
    #         if clip_denoised:
    #             return x.clamp(-1, 1)
    #         return x

    #     if self.model_mean_type == ModelMeanType.PREVIOUS_X:
    #         pred_xstart = process_xstart(
    #             self._predict_xstart_from_xprev(x_t=x, t=t, xprev=model_output)
    #         )
    #         model_mean = model_output
    #     elif self.model_mean_type in [ModelMeanType.START_X, ModelMeanType.EPSILON]:
    #         if self.model_mean_type == ModelMeanType.START_X:
    #             pred_xstart = process_xstart(model_output)
    #         else:
    #             pred_xstart = process_xstart(
    #                 self._predict_xstart_from_eps(x_t=x, t=t, eps=model_output)
    #             )
    #         model_mean, _, _ = self.q_posterior_mean_variance(
    #             x_start=pred_xstart, x_t=x, t=t
    #         )
    #     else:
    #         raise NotImplementedError(self.model_mean_type)

    #     assert (
    #         model_mean.shape == model_log_variance.shape == pred_xstart.shape == x.shape
    #     )
    #     return {
    #         "mean": model_mean,
    #         "variance": model_variance,
    #         "log_variance": model_log_variance,
    #         "pred_xstart": pred_xstart,
    #         'cal': cal,
    #     }


    def p_mean_variance(
        self, model, x, t, clip_denoised=True, denoised_fn=None, model_kwargs=None
    ):
        """
        用模型预测 p(x_{t-1}|x_t)，并返回:
        mean, variance, log_variance, pred_xstart。
        假设:
        - x.shape = [N, C_total, H, W] (其中最后一通道是要扩散的目标通道)
        - model_output 只针对最后一通道做预测 (比如1通道或2通道(含方差))
        """
        if model_kwargs is None:
            model_kwargs = {}
        B, C_total = x.shape[:2]  # 不再强行 C=1
        assert t.shape == (B,)

        # 仅拿最后一通道做扩散/还原
        seg_x = x[:, -1:, ...]  # [N,1,H,W], 这里是你真正要预测的“目标通道”
        
        # 1) 前向网络：把整张图（多通道）作为输入，使模型能利用条件通道信息
        #    你的网络若只输出( B,1,H,W ) or (B,2,H,W), 则与 seg_x 匹配.
        model_output = model(x, self._scale_timesteps(t), **model_kwargs)
        cal = 0
        if isinstance(model_output, tuple):
            # 如果模型额外输出了 cal 等校正信息，则解包
            model_output, cal = model_output  # cal.shape 通常 = [N,1,H,W]

        # 2) 若使用 LEARNED 或 LEARNED_RANGE，模型会输出 (B,2,H,W): (pred, var)
        if self.model_var_type in [ModelVarType.LEARNED, ModelVarType.LEARNED_RANGE]:
            # 比如 model_output.shape = (B, 2, H, W)
            # 第一个通道是预测值(可能是epsilon / x_0 / x_{t-1}), 第二个是 log_variance
            assert model_output.shape[1] == 2, \
                f"model_output 通道数不对, 期望2, 实际 {model_output.shape[1]}"
            model_output, model_var_values = th.split(model_output, 1, dim=1)
            # 处理 log_variance
            if self.model_var_type == ModelVarType.LEARNED:
                model_log_variance = model_var_values
                model_variance = th.exp(model_log_variance)
            else:
                # Learned_range时, 需插值到 [posterior_log_variance_clipped, log_beta]
                min_log = _extract_into_tensor(
                    self.posterior_log_variance_clipped, t, seg_x.shape
                )
                max_log = _extract_into_tensor(np.log(self.betas), t, seg_x.shape)
                # model_var_values ∈ [-1,1], 将其线性映射到 [min_log, max_log]
                frac = (model_var_values + 1) / 2
                model_log_variance = frac * max_log + (1 - frac) * min_log
                model_variance = th.exp(model_log_variance)
        else:
            # 如果是 FIXED_SMALL / FIXED_LARGE，则不从模型学 variance
            model_variance, model_log_variance = {
                ModelVarType.FIXED_LARGE: (
                    np.append(self.posterior_variance[1], self.betas[1:]),
                    np.log(np.append(self.posterior_variance[1], self.betas[1:])),
                ),
                ModelVarType.FIXED_SMALL: (
                    self.posterior_variance,
                    self.posterior_log_variance_clipped,
                ),
            }[self.model_var_type]
            model_variance = _extract_into_tensor(model_variance, t, seg_x.shape)
            model_log_variance = _extract_into_tensor(model_log_variance, t, seg_x.shape)

        # 3) 处理 pred_xstart
        def process_xstart(x_0):
            if denoised_fn is not None:
                x_0 = denoised_fn(x_0)
            if clip_denoised:
                x_0 = x_0.clamp(-1, 1)
            return x_0

        # 根据你设置的 model_mean_type 来反推 pred_xstart
        if self.model_mean_type == ModelMeanType.PREVIOUS_X:
            # 直接输出 x_{t-1}
            pred_xstart = process_xstart(
                self._predict_xstart_from_xprev(seg_x, t, model_output)
            )
            model_mean = model_output
        elif self.model_mean_type == ModelMeanType.START_X:
            # 直接输出 x_0
            pred_xstart = process_xstart(model_output)
            model_mean, _, _ = self.q_posterior_mean_variance(
                x_start=pred_xstart, x_t=seg_x, t=t
            )
        elif self.model_mean_type == ModelMeanType.EPSILON:
            # 输出的 model_output 是 epsilon
            pred_xstart = process_xstart(
                self._predict_xstart_from_eps(seg_x, t, model_output)
            )
            model_mean, _, _ = self.q_posterior_mean_variance(
                x_start=pred_xstart, x_t=seg_x, t=t
            )
        else:
            raise NotImplementedError(self.model_mean_type)

        # 保证形状一致：mean/logvar/pred_xstart均与 seg_x 的形状相同
        assert (
            model_mean.shape == model_log_variance.shape == pred_xstart.shape == seg_x.shape
        ), "p_mean_variance: 形状不一致!"

        return {
            "mean": model_mean,
            "variance": model_variance,
            "log_variance": model_log_variance,
            "pred_xstart": pred_xstart,  # 最后一通道 (N,1,H,W)
            "cal": cal,                  # 可能是额外的校正张量
        }


    def _predict_xstart_from_eps(self, x_t, t, eps):
        assert x_t.shape == eps.shape
        return (
            _extract_into_tensor(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t
            - _extract_into_tensor(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape) * eps
        )

    def _predict_xstart_from_xprev(self, x_t, t, xprev):
        assert x_t.shape == xprev.shape
        return (  # (xprev - coef2*x_t) / coef1
            _extract_into_tensor(1.0 / self.posterior_mean_coef1, t, x_t.shape) * xprev
            - _extract_into_tensor(
                self.posterior_mean_coef2 / self.posterior_mean_coef1, t, x_t.shape
            )
            * x_t
        )

    def _predict_eps_from_xstart(self, x_t, t, pred_xstart):
        return (
            _extract_into_tensor(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t
            - pred_xstart
        ) / _extract_into_tensor(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape)

    def _scale_timesteps(self, t):
        if self.rescale_timesteps:

            return t.float() * (1000.0 / self.num_timesteps)
        return t

    def condition_mean(self, cond_fn, p_mean_var, x, t, org, model_kwargs=None):
        """
        Compute the mean for the previous step, given a function cond_fn that
        computes the gradient of a conditional log probability with respect to
        x. In particular, cond_fn computes grad(log(p(y|x))), and we want to
        condition on y.
        This uses the conditioning strategy from Sohl-Dickstein et al. (2015).
        """
        a, gradient = cond_fn(x, self._scale_timesteps(t),org,  **model_kwargs)


        new_mean = (
            p_mean_var["mean"].float() + p_mean_var["variance"] * gradient.float()
        )
        return a, new_mean

    def condition_score(self, cond_fn, p_mean_var, x, t,  model_kwargs=None):
        """
        Compute what the p_mean_variance output would have been, should the
        model's score function be conditioned by cond_fn.
        See condition_mean() for details on cond_fn.
        Unlike condition_mean(), this instead uses the conditioning strategy
        from Song et al (2020).
        """
        alpha_bar = _extract_into_tensor(self.alphas_cumprod, t, x.shape)

        eps = self._predict_eps_from_xstart(x, t, p_mean_var["pred_xstart"])

        eps = eps.detach() - (1 - alpha_bar).sqrt() *p_mean_var["update"]*0

        out = p_mean_var.copy()
        out["pred_xstart"] = self._predict_xstart_from_eps(x.detach(), t.detach(), eps)
        out["mean"], _, _ = self.q_posterior_mean_variance(
            x_start=out["pred_xstart"], x_t=x, t=t
        )
        return out, eps


    def sample_known(self, img, batch_size = 1):
        image_size = self.image_size
        channels = self.channels
        return self.p_sample_loop_known(model,(batch_size, channels, image_size, image_size), img)

    def p_sample(
        self,
        model,
        x,
        t,
        clip_denoised=True,
        denoised_fn=None,
        model_kwargs=None,
    ):
        """
        Sample x_{t-1} from the model at the given timestep.
        :param model: the model to sample from.
        :param x: the current tensor at x_{t-1}.
        :param t: the value of t, starting at 0 for the first diffusion step.
        :param clip_denoised: if True, clip the x_start prediction to [-1, 1].
        :param denoised_fn: if not None, a function which applies to the
            x_start prediction before it is used to sample.
        :param cond_fn: if not None, this is a gradient function that acts
                        similarly to the model.
        :param model_kwargs: if not None, a dict of extra keyword arguments to
            pass to the model. This can be used for conditioning.
        :return: a dict containing the following keys:
                 - 'sample': a random sample from the model.
                 - 'pred_xstart': a prediction of x_0.
        """
        out = self.p_mean_variance(
            model,
            x,
            t,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            model_kwargs=model_kwargs,
        )
        noise = th.randn_like(x[:, -1:,...])
        nonzero_mask = (
            (t != 0).float().view(-1, *([1] * (len(x.shape) - 1)))
        )
        sample = out["mean"] + nonzero_mask * th.exp(0.5 * out["log_variance"]) * noise

        return {"sample": sample, "pred_xstart": out["pred_xstart"], "cal": out["cal"]}

    def p_sample_loop(
        self,
        model,
        shape,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,

    ):
        """
        Generate samples from the model.
        :param model: the model module.
        :param shape: the shape of the samples, (N, C, H, W).
        :param noise: if specified, the noise from the encoder to sample.
                      Should be of the same shape as `shape`.
        :param clip_denoised: if True, clip x_start predictions to [-1, 1].
        :param denoised_fn: if not None, a function which applies to the
            x_start prediction before it is used to sample.
        :param cond_fn: if not None, this is a gradient function that acts
                        similarly to the model.
        :param model_kwargs: if not None, a dict of extra keyword arguments to
            pass to the model. This can be used for conditioning.
        :param device: if specified, the device to create the samples on.
                       If not specified, use a model parameter's device.
        :param progress: if True, show a tqdm progress bar.
        :return: a non-differentiable batch of samples.
        """
        final = None
        for sample in self.p_sample_loop_progressive(
            model,
            shape,
            noise=noise,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            cond_fn=cond_fn,
            model_kwargs=model_kwargs,
            device=device,
            progress=progress,
        ):
            final = sample
        return final["sample"]




    def p_sample_loop_known_1(
        self,
        model,
        shape,
        img,
        step=50,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
    ):
        if device is None:
            device = next(model.parameters()).device

        img = img.to(device).float()

        if noise is None:
            noise = torch.randn_like(img[:, -1:, ...], device=device)

        x_noisy = torch.cat((img[:, :-1, ...], noise), dim=1).to(device)

        final = None
        for sample in self.ddim_sample_loop_progressive(
            model,
            shape,
            time=step,
            noise=x_noisy,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            cond_fn=cond_fn,
            model_kwargs=model_kwargs,
            device=device,
            progress=progress,
            eta=0.0,  # 确保使用确定性采样
        ):
            final = sample

        if final is None:
            raise RuntimeError("DDIM sampling did not produce any output.")

        final_sample = final["sample"]
        # 移除对cal的混合操作
        cal_out = final_sample[:, -1:, :, :]

        return final_sample, x_noisy, img, final["cal"], cal_out




    def p_sample_loop_known_0(
        self,
        model,
        shape,
        img,
        step=1000,
        org=None,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
        conditioner=None,
        classifier=None
    ):
        if device is None:
            device = next(model.parameters()).device
        img = img.to(device).float()
        
        # 确保输入图像通道数正确
        print("[DEBUG] Input img shape:", img.shape)  # 检查输入维度
        
        # 生成初始噪声（仅针对分割通道）
        if noise is None:
            noise = th.randn_like(img[:, -1:, ...], device=device)
        
        # 拼接条件信息（前N-1通道）和噪声（最后一通道）
        x_noisy = th.cat([img[:, :-1, ...], noise], dim=1)  # [N, total_channels, H, W]

        final = None
        if self.dpm_solver:
            # 初始化DPM Solver相关组件
            noise_schedule = NoiseScheduleVP(
                schedule='discrete', 
                betas=th.from_numpy(self.betas).to(device=device, dtype=th.float32)
            )

            # 包装模型以适配DPM Solver
            model_fn = model_wrapper(
                model,
                noise_schedule,
                model_type="noise",
                model_kwargs=model_kwargs,
            )

            # 初始化DPM Solver，传入条件信息（前N-1通道）
            dpm_solver = DPM_Solver(
                model_fn, 
                noise_schedule,
                algorithm_type="dpmsolver++",
                correcting_x0_fn="dynamic_thresholding",
                img=img[:, :-1, ...]  # 仅传入条件通道
            )

            # 执行采样过程
            sample, cal = dpm_solver.sample(
                noise.to(dtype=th.float32),
                steps=step,
                order=2,
                skip_type="time_uniform",
                method="multistep",
            )
            
            # 后处理
            sample = sample.detach()
            if sample.dim() == 3:  # 确保输出为4D张量
                sample = sample.unsqueeze(1)
            sample[:,-1,:,:] = norm(sample[:,-1,:,:])  # 归一化分割通道
            
            final = {"sample": sample, "cal": cal}
        else:
            # 原有非DPM Solver逻辑
            pass

        # 结果后处理
        final_sample = final["sample"]
        cal_out = final["cal"]
        
        # 根据需求混合结果
        if dice_score(final_sample[:,-1,:,:].unsqueeze(1), cal_out) < 0.65:
            cal_out = th.clamp(cal_out + 0.25 * final_sample[:,-1,:,:].unsqueeze(1), 0, 1)
        else:
            cal_out = th.clamp(cal_out * 0.5 + 0.5 * final_sample[:,-1,:,:].unsqueeze(1), 0, 1)

        return final_sample, x_noisy, img, final["cal"], cal_out






    def p_sample_loop_known(
        self,
        model,
        shape,
        img,
        step = 1000,
        org=None,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
        conditioner = None,
        classifier=None
    ):
        if device is None:
            device = next(model.parameters()).device
        assert isinstance(shape, (tuple, list))
        img = img.to(device)
        noise = th.randn_like(img[:, :1, ...]).to(device)
        x_noisy = torch.cat((img[:, :-1,  ...], noise), dim=1)  #add noise as the last channel
        
       


        img=img.to(device)

        # if self.dpm_solver:
        #     final = {}
        #     noise_schedule = NoiseScheduleVP(schedule='discrete', betas= th.from_numpy(self.betas))

        #     model_fn = model_wrapper(
        #         model,
        #         noise_schedule,
        #         model_type="noise",  # or "x_start" or "v" or "score"
        #         model_kwargs=model_kwargs,
        #     )

        #     dpm_solver = DPM_Solver(model_fn, noise_schedule, algorithm_type="dpmsolver++",
        #                     correcting_x0_fn="dynamic_thresholding", img = img[:, :-1,  ...])

        #     ## Steps in [20, 30] can generate quite good samples.
        #     sample, cal = dpm_solver.sample(
        #         noise.to(dtype=th.float),
        #         steps= step,
        #         order=2,
        #         skip_type="time_uniform",
        #         method="multistep",
        #     )
        #     sample = sample.detach()    ### MODIFIED: for DPM-Solver OOM issue
        #     sample[:,-1,:,:] = norm(sample[:,-1,:,:])
        #     final["sample"] = sample
        #     final["cal"] = cal

        #     cal_out = torch.clamp(final["cal"] + 0.25 * final["sample"][:,-1,:,:].unsqueeze(1), 0, 1)

        # 修改后的 DPM_Solver 初始化部分
        if self.dpm_solver:
            final = {}
            # 确保输入图像通道数正确
            print("[DEBUG] Input img shape:", img.shape)  # 检查输入维度
            
            noise_schedule = NoiseScheduleVP(
                schedule='discrete', 
                betas=th.from_numpy(self.betas).to(device=img.device, dtype=th.float32)
            )

            model_fn = model_wrapper(
                model,
                noise_schedule,
                model_type="noise",
                model_kwargs=model_kwargs,
            )

            # 方法一：截取前N个通道（例如2个通道）
            adjusted_img = img[:, :2, ...]  # 调整此处索引以匹配模型需求
            
            dpm_solver = DPM_Solver(
                model_fn, 
                noise_schedule,
                algorithm_type="dpmsolver++",
                correcting_x0_fn="dynamic_thresholding",
                img=adjusted_img  # 使用调整后的图像
            )

            sample, cal = dpm_solver.sample(
                noise.to(dtype=th.float32),
                steps=step,
                order=2,
                skip_type="time_uniform",
                method="multistep",
            )
            
            # 后续处理保持不变...
            sample = sample.detach()
            if sample.shape[1] > 1:  # 多通道归一化处理
                sample[:, -1, :, :] = norm(sample[:, -1, :, :])
            else:
                sample = norm(sample)
            final["sample"] = sample
            final["cal"] = cal


        else:
            print('no dpm-solver')
            i = 0
            letters = string.ascii_lowercase
            name = ''.join(random.choice(letters) for i in range(10)) 
            for sample in self.p_sample_loop_progressive(
                model,
                shape,
                time = step,
                noise=x_noisy,
                clip_denoised=clip_denoised,
                denoised_fn=denoised_fn,
                cond_fn=cond_fn,
                org=org,
                model_kwargs=model_kwargs,
                device=device,
                progress=progress,
            ):
                final = sample
                # i += 1
                # '''vis each step sample'''
                # if i % 5 == 0:

                #     o1 = th.tensor(img)[:,0,:,:].unsqueeze(1)
                #     o2 = th.tensor(img)[:,1,:,:].unsqueeze(1)
                #     o3 = th.tensor(img)[:,2,:,:].unsqueeze(1)
                #     o4 = th.tensor(img)[:,3,:,:].unsqueeze(1)
                #     s = th.tensor(final["sample"])[:,-1,:,:].unsqueeze(1)
                #     tup = (o1/o1.max(),o2/o2.max(),o3/o3.max(),o4/o4.max(),s)
                #     compose = th.cat(tup,0)
                #     vutils.save_image(s, fp = os.path.join('../res_temp_norm_6000_100', name+str(i)+".jpg"), nrow = 1, padding = 10)

            if dice_score(final["sample"][:,-1,:,:].unsqueeze(1), final["cal"]) < 0.65:
                cal_out = torch.clamp(final["cal"] + 0.25 * final["sample"][:,-1,:,:].unsqueeze(1), 0, 1)
            else:
                cal_out = torch.clamp(final["cal"] * 0.5 + 0.5 * final["sample"][:,-1,:,:].unsqueeze(1), 0, 1)
            

        return final["sample"], x_noisy, img, final["cal"], cal_out

    def p_sample_loop_progressive(
        self,
        model,
        shape,
        time=1000,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        org=None,
        model_kwargs=None,
        device=None,
        progress=False,
        ):
        """
        Generate samples from the model and yield intermediate samples from
        each timestep of diffusion.
        Arguments are the same as p_sample_loop().
        Returns a generator over dicts, where each dict is the return value of
        p_sample().
        """

        if device is None:
            device = next(model.parameters()).device
        assert isinstance(shape, (tuple, list))
        if noise is not None:
            img = noise
        else:
            img = th.randn(*shape, device=device)
        indices = list(range(time))[::-1]
        org_c = img.size(1)
        org_MRI = img[:, :-1, ...]      #original brain MR image
        if progress:
            # Lazy import so that we don't depend on tqdm.
            from tqdm.auto import tqdm

            indices = tqdm(indices)

        else:
           for i in indices:
                t = th.tensor([i] * shape[0], device=device)
                # if i%100==0:
                    # print('sampling step', i)
                    # viz.image(visualize(img.cpu()[0, -1,...]), opts=dict(caption="sample"+ str(i) ))

                with th.no_grad():
                    # print('img bef size',img.size())
                    if img.size(1) != org_c:
                        img = torch.cat((org_MRI,img), dim=1)       #in every step, make sure to concatenate the original image to the sampled segmentation mask

                    out = self.p_sample(
                        model,
                        img.float(),
                        t,
                        clip_denoised=clip_denoised,
                        denoised_fn=denoised_fn,
                        model_kwargs=model_kwargs,
                    )
                    yield out
                    img = out["sample"]

    def ddim_sample(
            self,
            model,
            x,
            t,
            clip_denoised=True,
            denoised_fn=None,
            cond_fn=None,
            model_kwargs=None,
            eta=0.0,
    ):
        """
        Sample x_{t-1} from the model using DDIM.
        Same usage as p_sample().
        """
        out = self.p_mean_variance(
            model,
            x,
            t,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            model_kwargs=model_kwargs,
        )


        if cond_fn is not None:
            out = self.condition_score(cond_fn, out, x, t, model_kwargs=model_kwargs)

        # Usually our model outputs epsilon, but we re-derive it
        # in case we used x_start or x_prev prediction.
        eps = self._predict_eps_from_xstart(x, t, out["pred_xstart"])

        alpha_bar = _extract_into_tensor(self.alphas_cumprod, t, x.shape)
        alpha_bar_prev = _extract_into_tensor(self.alphas_cumprod_prev, t, x.shape)
        sigma = (
                eta
                * th.sqrt((1 - alpha_bar_prev) / (1 - alpha_bar))
                * th.sqrt(1 - alpha_bar / alpha_bar_prev)
        )
        # Equation 12.
        noise = th.randn_like(x[:, -1:, ...])

        mean_pred = (
                out["pred_xstart"] * th.sqrt(alpha_bar_prev)
                + th.sqrt(1 - alpha_bar_prev - sigma ** 2) * eps
        )
        nonzero_mask = (
            (t != 0).float().view(-1, *([1] * (len(x.shape) - 1)))
        )  # no noise when t == 0
        sample = mean_pred + nonzero_mask * sigma * noise
        return {"sample": sample, "pred_xstart": out["pred_xstart"]}


    # def ddim_reverse_sample(
    #     self,
    #     model,
    #     x,
    #     t,
    #     clip_denoised=True,
    #     denoised_fn=None,
    #     model_kwargs=None,
    #     eta=0.0,
    # ):
    #     """
    #     Sample x_{t+1} from the model using DDIM reverse ODE.
    #     """
    #     assert eta == 0.0, "Reverse ODE only for deterministic path"
    #     out = self.p_mean_variance(
    #         model,
    #         x,
    #         t,
    #         clip_denoised=clip_denoised,
    #         denoised_fn=denoised_fn,
    #         model_kwargs=model_kwargs,
    #     )
    #     # Usually our model outputs epsilon, but we re-derive it
    #     # in case we used x_start or x_prev prediction.
    #     eps = (
    #         _extract_into_tensor(self.sqrt_recip_alphas_cumprod, t, x.shape) * x
    #         - out["pred_xstart"]
    #     ) / _extract_into_tensor(self.sqrt_recipm1_alphas_cumprod, t, x.shape)
    #     alpha_bar_next = _extract_into_tensor(self.alphas_cumprod_next, t, x.shape)

    #     # Equation 12. reversed
    #     mean_pred = (
    #         out["pred_xstart"] * th.sqrt(alpha_bar_next)
    #         + th.sqrt(1 - alpha_bar_next) * eps
    #     )

    #     return {"sample": mean_pred, "pred_xstart": out["pred_xstart"]}


#     def ddim_sample(
#     self,
#     model,
#     x,
#     t,
#     clip_denoised=True,
#     denoised_fn=None,
#     cond_fn=None,
#     model_kwargs=None,
#     eta=0.0,
# ):
#     # 计算均值和方差
#         out = self.p_mean_variance(
#             model,
#             x,
#             t,
#             clip_denoised=clip_denoised,
#             denoised_fn=denoised_fn,
#             model_kwargs=model_kwargs,
#         )

#         # 条件函数处理
#         if cond_fn is not None:
#             out = self.condition_score(cond_fn, out, x, t, model_kwargs=model_kwargs)
#             eps = self._predict_eps_from_xstart(x, t, out["pred_xstart"])  # 重新计算eps
#         else:
#             eps = self._predict_eps_from_xstart(x, t, out["pred_xstart"])

#         # 提取alpha参数
#         alpha_bar = _extract_into_tensor(self.alphas_cumprod, t, x.shape)
#         alpha_bar_prev = _extract_into_tensor(self.alphas_cumprod_prev, t, x.shape)
        
#         # 计算sigma（公式12）
#         sigma = (
#             eta
#             * th.sqrt((1 - alpha_bar_prev) / (1 - alpha_bar))
#             * th.sqrt(1 - alpha_bar / alpha_bar_prev)
#         )

#         # 生成与x同维度的噪声（关键修正）
#         noise = th.randn_like(x)  # 修正维度问题

#         # 计算均值预测
#         mean_pred = (
#             out["pred_xstart"] * th.sqrt(alpha_bar_prev)
#             + th.sqrt(1 - alpha_bar_prev - sigma ** 2) * eps
#         )

#         # 非零掩码（t=0时不加噪声）
#         nonzero_mask = (t != 0).float().view(-1, *([1] * (len(x.shape) - 1)))

#         # 最终采样结果
#         sample = mean_pred + nonzero_mask * sigma * noise
#         return {"sample": sample, "pred_xstart": out["pred_xstart"]}
    def ddim_sample_1(
        self,
        model,
        x,
        t,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        eta=0.0,
    ):
        # 将输入拆分为条件部分和去噪部分
        condition_part = x[:, :-1, ...]  # 前C-1个通道作为条件
        seg_part = x[:, -1:, ...]       # 最后一个通道需要去噪

        # 计算均值和方差（仅处理最后一个通道）
        out = self.p_mean_variance(
            model,
            x,  # 模型需要条件信息，传入完整x
            t,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            model_kwargs=model_kwargs,
        )
        pred_xstart = out["pred_xstart"]  # 应只包含最后一个通道的预测

        # 处理条件函数（如果有）
        if cond_fn is not None:
            out = self.condition_score(cond_fn, out, x, t, model_kwargs=model_kwargs)

        # 计算epsilon（仅针对最后一个通道）
        eps = self._predict_eps_from_xstart(seg_part, t, pred_xstart)

        # 提取alpha参数（维度对齐seg_part）
        alpha_bar = _extract_into_tensor(self.alphas_cumprod, t, seg_part.shape)
        alpha_bar_prev = _extract_into_tensor(self.alphas_cumprod_prev, t, seg_part.shape)

        # 计算sigma
        sigma = (
            eta
            * th.sqrt((1 - alpha_bar_prev) / (1 - alpha_bar))
            * th.sqrt(1 - alpha_bar / alpha_bar_prev)
        )

        # 生成噪声（仅针对最后一个通道）
        noise = th.randn_like(seg_part)

        # 计算均值预测
        mean_pred = (
            pred_xstart * th.sqrt(alpha_bar_prev)
            + th.sqrt(1 - alpha_bar_prev - sigma**2) * eps
        )

        # 非零掩码（t=0时不加噪声）
        nonzero_mask = (t != 0).float().view(-1, *([1] * (len(x.shape) - 1)))

        # 更新分割通道
        new_seg = mean_pred + nonzero_mask * sigma * noise

        # 拼接条件部分和新生成的分割
        sample = th.cat([condition_part, new_seg], dim=1)

        return {"sample": sample, "pred_xstart": pred_xstart}

    def ddim_sample(
    self,
    model,
    x,
    t,
    clip_denoised=True,
    denoised_fn=None,
    cond_fn=None,
    model_kwargs=None,
    eta=0.0,
):
        """
        对输入 x 在时刻 t 使用 DDIM 更新"最后一个通道"。
        x.shape = [N, condition_channels + 1, H, W]
        """
        if model_kwargs is None:
            model_kwargs = {}

        # 1) 拿到p(x_{t-1}|x_t)各项
        out = self.p_mean_variance(
            model,
            x,
            t,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            model_kwargs=model_kwargs,
        )
        pred_xstart = out["pred_xstart"]  # [N,1,H,W], 只对最后通道做预测
        cal_map = out.get("cal", torch.zeros_like(pred_xstart))

        # 2) 根据 cond_fn 做条件 (若有)
        if cond_fn is not None:
            out = self.condition_score(cond_fn, out, x, t, model_kwargs=model_kwargs)

        # 3) 重新推导 epsilon
        seg_x = x[:, -1:, ...]  # 最后一通道
        eps = self._predict_eps_from_xstart(seg_x, t, pred_xstart)

        # 4) 计算 DDIM 参数
        alpha_bar     = _extract_into_tensor(self.alphas_cumprod,      t, seg_x.shape)
        alpha_bar_prev= _extract_into_tensor(self.alphas_cumprod_prev, t, seg_x.shape)

        sigma = (
            eta
            * torch.sqrt((1 - alpha_bar_prev) / (1 - alpha_bar))
            * torch.sqrt(1 - alpha_bar / alpha_bar_prev)
        )

        # 5) 公式 (12) 的 deterministic + noise 部分
        mean_pred = (
            pred_xstart * torch.sqrt(alpha_bar_prev)
            + torch.sqrt(1 - alpha_bar_prev - sigma**2) * eps
        )
        nonzero_mask = (t != 0).float().view(-1, 1, 1, 1)
        noise = torch.randn_like(seg_x)
        new_seg = mean_pred + nonzero_mask * sigma * noise

        # 6) 把 "其他通道" + "更新后的分割通道" 拼回去
        condition_part = x[:, :-1, ...]
        x_updated = torch.cat([condition_part, new_seg], dim=1)

        return {
            "sample": x_updated,        # [N, condition_channels+1, H, W]
            "pred_xstart": pred_xstart, # [N,1,H,W]
            "cal": cal_map,             # 同上
        }

    def ddim_sample_loop_known(
        self,
        model,
        shape,
        img,
        step=1000,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
    ):
        """
        演示如何对“多模态 MRI + 最后一通道 segmentation” 的图像进行 DDIM 推理：
          - 保留前面通道不变
          - 对最后通道随机初始化后，一步步反推

        shape: (N, total_channels, H, W)
        img:   (N, total_channels, H, W)，其中最后一通道是 GT / 标注 / 或原来的初始化。
        noise: 若不为 None，就替换最后一通道为 noise，否则随机生成。
        """
        if device is None:
            device = next(model.parameters()).device

        img = img.to(device).float()     # 原图

        # 如果未指定 noise，就随机生成与最后一通道同形状的噪声
        if noise is None:
            noise = torch.randn_like(img[:, -1:, ...], device=device)

        # 构造初始带噪输入 x_noisy：前面通道为原 MRI，最后通道替换为 noise
        x_noisy = torch.cat((img[:, :-1, ...], noise), dim=1)

        # 循环调用 ddim_sample_loop_progressive 得到最终采样结果
        final = None
        
        for sample in self.ddim_sample_loop_progressive(
            model,
            shape=shape,
            time=step,
            noise=x_noisy,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            cond_fn=cond_fn,
            model_kwargs=model_kwargs,
            device=device,
            progress=progress,
            eta=0.0,  # 根据需要可修改
        ):
            final = sample  # 不断更新，直到最后一次

        if final is None:
            raise RuntimeError("DDIM sampling did not produce any output.")

        final_sample = final["sample"]  # [N, total_channels, H, W]
        final_cal    = final.get("cal", torch.zeros_like(final_sample[:, -1:, ...]))

        # 也可以根据 dice_score 或其它指标来融合 final_sample 与 final_cal
        # 例如：
        pred_mask = final_sample[:, -1, :, :].unsqueeze(1)
        score = dice_score(pred_mask, final_cal)
        if score < 0.65:
            cal_out = torch.clamp(final_cal + 0.25 * pred_mask, 0, 1)
        else:
            cal_out = torch.clamp(final_cal * 0.5 + 0.5 * pred_mask, 0, 1)

        return final_sample, x_noisy, img, final_cal, cal_out

    def ddim_sample_loop_interpolation(
        self,
        model,
        shape,
        img1,
        img2,
        lambdaint,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
    ):
        if device is None:
            device = next(model.parameters()).device
        assert isinstance(shape, (tuple, list))
        b = shape[0]
        t = th.randint(499,500, (b,), device=device).long().to(device)

        img1=torch.tensor(img1).to(device)
        img2 = torch.tensor(img2).to(device)

        noise = th.randn_like(img1).to(device)
        x_noisy1 = self.q_sample(x_start=img1, t=t, noise=noise).to(device)
        x_noisy2 = self.q_sample(x_start=img2, t=t, noise=noise).to(device)
        interpol=lambdaint*x_noisy1+(1-lambdaint)*x_noisy2

        for sample in self.ddim_sample_loop_progressive(
            model,
            shape,
            time=t,
            noise=interpol,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            cond_fn=cond_fn,
            model_kwargs=model_kwargs,
            device=device,
            progress=progress,
        ):
            final = sample
        return final["sample"], interpol, img1, img2



    def ddim_sample_loop(
        self,
        model,
        shape,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
        eta=0.0,
    ):
        """
        Generate samples from the model using DDIM.
        Same usage as p_sample_loop().
        """
        final = None
        if device is None:
            device = next(model.parameters()).device
        assert isinstance(shape, (tuple, list))
        b = shape[0]
        t = th.randint(99, 100, (b,), device=device).long().to(device)

        for sample in self.ddim_sample_loop_progressive(
            model,
            shape,
            time=t,
            noise=noise,
            clip_denoised=clip_denoised,
            denoised_fn=denoised_fn,
            cond_fn=cond_fn,
            model_kwargs=model_kwargs,
            device=device,
            progress=progress,
            eta=eta,
        ):

            final = sample
       # viz.image(visualize(final["sample"].cpu()[0, ...]), opts=dict(caption="sample"+ str(10) ))
        return final["sample"]



    # 函数逻辑...
    # def ddim_sample_loop_known(
    #         self,
    #         model,
    #         shape,
    #         img,
    #         clip_denoised=True,
    #         denoised_fn=None,
    #         cond_fn=None,
    #         model_kwargs=None,
    #         device=None,
    #         progress=False,
    #         eta = 0.0
    # ):
        # if device is None:
        #     device = next(model.parameters()).device
        # assert isinstance(shape, (tuple, list))
        # b = shape[0]

        # img = img.to(device)

        # t = th.randint(499,500, (b,), device=device).long().to(device)
        # noise = th.randn_like(img[:, :1, ...]).to(device)

        # x_noisy = torch.cat((img[:, :-1, ...], noise), dim=1).float()
        # img = img.to(device)

        # final = None
        # for sample in self.ddim_sample_loop_progressive(
        #     model,
        #     shape,
        #     time=t,
        #     noise=x_noisy,
        #     clip_denoised=clip_denoised,
        #     denoised_fn=denoised_fn,
        #     cond_fn=cond_fn,
        #     model_kwargs=model_kwargs,
        #     device=device,
        #     progress=progress,
        #     eta=eta,
        # ):
        #     final = sample

        # return final["sample"], x_noisy, img



    def ddim_sample_loop_progressive(
    self,
    model,
    shape,
    time=50,
    noise=None,
    clip_denoised=True,
    denoised_fn=None,
    cond_fn=None,
    model_kwargs=None,
    device=None,
    progress=False,
    eta=0.0,
):
        if device is None:
            device = next(model.parameters()).device

        # 修改初始化，确保保留多通道条件信息
        if noise is not None:
            img = noise.to(device)
        else:
            # 假设 shape = (N, total_channels, H, W)
            # 前面的 total_channels - 1 是条件通道，最后1个是需要去噪的分割通道
            N, total_channels, H, W = shape
            condition_channels = total_channels - 1
            
            # 在此处，应使用你的实际条件数据，而非全零或随机数据
            # 比如，如果你有固定的MRI条件图像，可以这样做：
            # condition_part = 已知的 MRI 图像 [N, condition_channels, H, W]
            # 这里暂时用zeros来示例
            condition_part = torch.zeros((N, condition_channels, H, W), device=device)
            
            segmentation_noise = torch.randn((N, 1, H, W), device=device)
            img = torch.cat([condition_part, segmentation_noise], dim=1)

        # 以下部分保持不变
        total_steps = self.num_timesteps
        step_indices = np.linspace(0, total_steps - 1, time, dtype=int)
        indices = list(step_indices[::-1])
        if indices[-1] != 0:
            indices.append(0)

        if progress:
            from tqdm.auto import tqdm
            indices = tqdm(indices)

        for i in indices:
            t = torch.tensor([i] * shape[0], device=device)
            with torch.no_grad():
                out = self.ddim_sample(
                    model,
                    img,
                    t,
                    clip_denoised=clip_denoised,
                    denoised_fn=denoised_fn,
                    cond_fn=cond_fn,
                    model_kwargs=model_kwargs,
                    eta=eta,
                )
            yield out
            img = out["sample"]

    def ddim_sample_loop_progressive_1(
        self,
        model,
        shape,
        time=50,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
        eta=0.0,  # 默认确定性采样
    ):
        if device is None:
            device = next(model.parameters()).device

        if noise is not None:
            img = noise.to(device)
        else:
            img = torch.randn(*shape, device=device)

        total_steps = self.num_timesteps
        step_indices = np.linspace(0, total_steps - 1, time, dtype=int)
        indices = list(step_indices[::-1])  # 倒序处理

        # 确保包含t=0
        if indices[-1] != 0:
            indices.append(0)

        if progress:
            from tqdm.auto import tqdm
            indices = tqdm(indices)

        for i in indices:
            t = torch.tensor([i] * shape[0], device=device)
            with torch.no_grad():
                out = self.ddim_sample(
                    model,
                    img,
                    t,
                    clip_denoised=clip_denoised,
                    denoised_fn=denoised_fn,
                    cond_fn=cond_fn,
                    model_kwargs=model_kwargs,
                    eta=eta,
                )
            yield out
            img = out["sample"]




    def ddim_sample_loop_progressive_1(
    self,
    model,
    shape,
    time=50,  # 用户指定的步数，例如50
    noise=None,
    clip_denoised=True,
    denoised_fn=None,
    cond_fn=None,
    model_kwargs=None,
    device=None,
    progress=False,
    eta=0.0,  # 确保eta=0以去除随机噪声
):
        """
        使用 DDIM 逐步采样，生成中间结果。
        :param model: 模型实例。
        :param shape: 输出张量的形状，例如 (N, C, H, W)。
        :param time: 采样步数，默认为50。
        :param noise: 初始噪声张量，若未提供则随机生成。
        :param clip_denoised: 是否将去噪结果裁剪到[-1, 1]。
        :param denoised_fn: 可选的去噪后处理函数。
        :param cond_fn: 可选的条件函数。
        :param model_kwargs: 传递给模型的额外参数。
        :param device: 使用的设备（如CPU或GPU）。
        :param progress: 是否显示进度条。
        :param eta: DDIM的随机噪声参数，设置为0以确保确定性采样。
        :return: 生成器，逐步返回采样结果。
        """
        if device is None:
            device = next(model.parameters()).device

        # 初始化噪声
        if noise is not None:
            img = noise.to(device)
        else:
            img = torch.randn(*shape, device=device)

        # 生成正确的时间步索引
        total_steps = self.num_timesteps  # 假设原始有1000步
        step_indices = np.linspace(0, total_steps - 1, time, dtype=int)
        
        indices = list(step_indices[::-1])  # 倒序处理
        if indices[-1] != 0:
            indices.append(0)

        if progress:
            from tqdm.auto import tqdm
            indices = tqdm(indices)

        # 逐步采样
        for i in indices:
            t = torch.tensor([i] * shape[0], device=device)
            with torch.no_grad():
                out = self.ddim_sample(
                    model,
                    img,
                    t,
                    clip_denoised=clip_denoised,
                    denoised_fn=denoised_fn,
                    cond_fn=cond_fn,
                    model_kwargs=model_kwargs,
                    eta=eta,  # 确保eta=0以去除随机噪声
                )
            yield out
            img = out["sample"]


    def ddim_sample_loop_progressive_0(
        self,
        model,
        shape,
        time=1000,
        noise=None,
        clip_denoised=True,
        denoised_fn=None,
        cond_fn=None,
        model_kwargs=None,
        device=None,
        progress=False,
        eta=0.0,
    ):
        """
        对给定形状 shape=[N, total_channels, H, W] 的批次数据进行
        DDIM 逐步推理，返回每一步的中间结果。
        注意：只在最后一通道执行扩散，还原，其它通道视作条件不变。
        """
        if device is None:
            device = next(model.parameters()).device

        # 初始 x，如果没有外部 noise，就随机初始化
        if noise is not None:
            img = noise.to(device)
        else:
            img = torch.randn(*shape, device=device)

        # 反向时间步列表
        indices = list(range(time))[::-1]

        if progress:
            from tqdm.auto import tqdm
            indices = tqdm(indices)

        # 逐步采样
        for i in indices:
            t = torch.tensor([i] * shape[0], device=device)
            with torch.no_grad():
                out = self.ddim_sample(
                    model,
                    img,
                    t,
                    clip_denoised=clip_denoised,
                    denoised_fn=denoised_fn,
                    cond_fn=cond_fn,
                    model_kwargs=model_kwargs,
                    eta=eta,
                )
            yield out
            img = out["sample"]

    # def ddim_sample_loop_progressive(
    #     self,
    #     model,
    #     shape,
    #     time=1000,
    #     noise=None,
    #     clip_denoised=True,
    #     denoised_fn=None,
    #     cond_fn=None,
    #     model_kwargs=None,
    #     device=None,
    #     progress=False,
    #     eta=0.0,
    # ):
    #     """
    #     Use DDIM to sample from the model and yield intermediate samples from
    #     each timestep of DDIM.
    #     Same usage as p_sample_loop_progressive().
    #     """
    #     if device is None:
    #         device = next(model.parameters()).device
    #     assert isinstance(shape, (tuple, list))
    #     if noise is not None:
    #         img = noise
    #     else:
    #         img = th.randn(*shape, device=device)
    #     indices = list(range(time-1))[::-1]
    #     orghigh = img[:, :-1, ...]


    #     if progress:
    #         # Lazy import so that we don't depend on tqdm.
    #         from tqdm.auto import tqdm

    #         indices = tqdm(indices)

    #     for i in indices:
    #             t = th.tensor([i] * shape[0], device=device)
    #             with th.no_grad():
    #             #  if img.shape != (1, 5, 224, 224):
    #             #      img = torch.cat((orghigh,img), dim=1).float()

    #              out = self.ddim_sample(
    #                 model,
    #                 img,
    #                 t,
    #                 clip_denoised=clip_denoised,
    #                 denoised_fn=denoised_fn,
    #                 cond_fn=cond_fn,
    #                 model_kwargs=model_kwargs,
    #                 eta=eta,
    #              )
    #             yield out
    #             img = out["sample"]

    def _vb_terms_bpd(
        self, model, x_start, x_t, t, clip_denoised=True, model_kwargs=None
    ):
        """
        Get a term for the variational lower-bound.
        The resulting units are bits (rather than nats, as one might expect).
        This allows for comparison to other papers.
        :return: a dict with the following keys:
                 - 'output': a shape [N] tensor of NLLs or KLs.
                 - 'pred_xstart': the x_0 predictions.
        """
        true_mean, _, true_log_variance_clipped = self.q_posterior_mean_variance(
            x_start=x_start, x_t=x_t, t=t
        )
        out = self.p_mean_variance(
            model, x_t, t, clip_denoised=clip_denoised, model_kwargs=model_kwargs
        )
        kl = normal_kl(
            true_mean, true_log_variance_clipped, out["mean"], out["log_variance"]
        )
        kl = mean_flat(kl) / np.log(2.0)

        decoder_nll = -discretized_gaussian_log_likelihood(
            x_start, means=out["mean"], log_scales=0.5 * out["log_variance"]
        )
        assert decoder_nll.shape == x_start.shape
        decoder_nll = mean_flat(decoder_nll) / np.log(2.0)

        # At the first timestep return the decoder NLL,
        # otherwise return KL(q(x_{t-1}|x_t,x_0) || p(x_{t-1}|x_t))
        output = th.where((t == 0), decoder_nll, kl)
        return {"output": output, "pred_xstart": out["pred_xstart"]}



    def training_losses_segmentation(self, model, classifier, x_start, t, model_kwargs=None, noise=None):
        """
        Compute training losses for a single timestep.
        :param model: the model to evaluate loss on.
        :param x_start: the [N x C x ...] tensor of inputs.
        :param t: a batch of timestep indices.
        :param model_kwargs: if not None, a dict of extra keyword arguments to
            pass to the model. This can be used for conditioning.
        :param noise: if specified, the specific Gaussian noise to try to remove.
        :return: a dict with the key "loss" containing a tensor of shape [N].
                 Some mean or variance settings may also have other keys.
        """
        if model_kwargs is None:
            model_kwargs = {}
        if noise is None:
            noise = th.randn_like(x_start[:, -1:, ...])


        mask = x_start[:, -1:, ...]
        res = mask.clone()   #merge all tumor classes into one to get a binary segmentation mask

        res_t = self.q_sample(res, t, noise=noise)     #add noise to the segmentation channel
        x_t = x_start.clone().float()
        x_t[:, 0,...] = x_start[:, 0,...] + 10*x_start[:, 1,...]

        x_t[:, -1:, ...]=res_t.float()
        terms = {}


        if self.loss_type == LossType.MSE or self.loss_type == LossType.BCE_DICE or self.loss_type == LossType.RESCALED_MSE:

            model_output, cal = model(x_t, self._scale_timesteps(t), **model_kwargs)
            if self.model_var_type in [
                ModelVarType.LEARNED,
                ModelVarType.LEARNED_RANGE,
            ]:
                B, C = x_t.shape[:2]
                C=1
                assert model_output.shape == (B, C * 2, *x_t.shape[2:])
                model_output, model_var_values = th.split(model_output, C, dim=1)
                # Learn the variance using the variational bound, but don't let
                # it affect our mean prediction.
                frozen_out = th.cat([model_output.detach(), model_var_values], dim=1)
                terms["vb"] = self._vb_terms_bpd(
                    model=lambda *args, r=frozen_out: r,
                    x_start=res,
                    x_t=res_t,
                    t=t,
                    clip_denoised=False,
                )["output"]
                if self.loss_type == LossType.RESCALED_MSE:
                    # Divide by 1000 for equivalence with initial implementation.
                    # Without a factor of 1/1000, the VB term hurts the MSE term.
                    terms["vb"] *= self.num_timesteps / 1000.0

            target = {
                ModelMeanType.PREVIOUS_X: self.q_posterior_mean_variance(
                    x_start=res, x_t=res_t, t=t
                )[0],
                ModelMeanType.START_X: res,
                ModelMeanType.EPSILON: noise,
            }[self.model_mean_type]

            loss_pinn = torch.tensor(self.cal_pinn(cal[:,0,:,:], x_t[:,0,:,:], x_t[:,1,:,:], k=0.2))
            loss_pinn = loss_pinn.to(x_t.device)
            

            # model_output = (cal > 0.5) * (model_output >0.5) * model_output if 2. * (cal*model_output).sum() / (cal+model_output).sum() < 0.75 else model_output
            # terms["loss_diff"] = nn.BCELoss(model_output, target)
            terms["loss_diff"] = mean_flat((target - model_output) ** 2 ) 
            terms["loss_cal"] = mean_flat((res - cal) ** 2) + loss_pinn
            # terms["loss_cal"] = nn.BCELoss()(cal.type(th.float), res.type(th.float)) 
            # terms["mse"] = (terms["mse_diff"] + terms["mse_cal"]) / 2.
            if "vb" in terms:
                terms["loss"] = terms["loss_diff"] + terms["vb"]
            else:
                terms["loss"] = terms["loss_diff"] 

        else:
            raise NotImplementedError(self.loss_type)

        return (terms, model_output)


    def cal_pinn(self, cal, buildings, shooter, k=1.0, k_building=1.0): # Adjusted default k_building
        """
        Calculates PINN loss batch-wise for path loss prediction based on dataset description
        where cal = 1.0 means LOW path loss (HIGH signal strength, near source)
        and cal = 0.0 means HIGH path loss (LOW signal strength, inside building/far away).

        Args:
            cal (np.ndarray or torch.Tensor): Predicted normalized signal strength level (f/255).
                                              Shape (bs, H, W). Values near 1 mean low PL, near 0 mean high PL.
            buildings (np.ndarray or torch.Tensor): Building mask. Shape (bs, H, W). 1 for building, 0 otherwise.
            shooter (np.ndarray or torch.Tensor): Source/transmitter location mask. Shape (bs, H, W). 1 for source, 0 otherwise.
            k (float): Heuristic parameter for wave-like behavior in free space. Needs tuning.
            k_building (float): Heuristic parameter for wave-like behavior inside buildings. Needs tuning.

        Returns:
            list: A list containing the calculated PINN loss for each item in the batch.
                  Note: Returning a list might not be ideal for backpropagation if using PyTorch/TensorFlow.
                  Consider returning the mean loss or stacking the losses into a tensor.
        """
        # --- Input Conversion ---
        if hasattr(cal, 'detach'):
            cal = cal.detach().cpu().numpy()
        if hasattr(buildings, 'detach'):
            buildings = buildings.detach().cpu().numpy()
        if hasattr(shooter, 'detach'):
            shooter = shooter.detach().cpu().numpy()

        bs, H, W = cal.shape
        loss_list = []
        # from scipy.ndimage import binary_erosion # Moved import to top

        for i in range(bs):
            cal_i = cal[i]
            buildings_i = buildings[i]
            shooter_i = shooter[i]

            # --- 1. PDE Loss (L_pde) ---
            lap = np.zeros_like(cal_i)
            lap[1:-1, 1:-1] = (
                cal_i[2:, 1:-1] + cal_i[:-2, 1:-1] +
                cal_i[1:-1, 2:] + cal_i[1:-1, :-2] -
                4 * cal_i[1:-1, 1:-1]
            )

            
            k_map = np.where(buildings_i == 1, k_building, k)

            # Calculate the PDE residual (Heuristic: ∇²(Signal) + k_map² * Signal ≈ 0)
            r = lap + (k_map ** 2) * cal_i
            L_pde = np.mean(r ** 2)

            # --- 2. Boundary Condition Loss (L_bc) ---
            buildings_mask = (buildings_i == 1)
            if np.any(buildings_mask):
                
                L_bc = np.mean((cal_i[buildings_mask]) ** 2)
            else:
                L_bc = 0.0 

            # --- 3. Source Condition Loss (L_source) ---
            shooter_mask = (shooter_i == 1)
            if np.any(shooter_mask):
                L_source = np.mean((cal_i[shooter_mask] - 1.0) ** 2)
            else:
                L_source = 0.0 
            # --- Total Loss for this sample ---
            
            loss = L_pde + 1.0 * L_bc + 1.0 * L_source 
            loss_list.append(loss)

        
        return loss_list


    


    def _prior_bpd(self, x_start):
        """
        Get the prior KL term for the variational lower-bound, measured in
        bits-per-dim.
        This term can't be optimized, as it only depends on the encoder.
        :param x_start: the [N x C x ...] tensor of inputs.
        :return: a batch of [N] KL values (in bits), one per batch element.
        """
        batch_size = x_start.shape[0]
        t = th.tensor([self.num_timesteps - 1] * batch_size, device=x_start.device)
        qt_mean, _, qt_log_variance = self.q_mean_variance(x_start, t)
        kl_prior = normal_kl(
            mean1=qt_mean, logvar1=qt_log_variance, mean2=0.0, logvar2=0.0
        )
        return mean_flat(kl_prior) / np.log(2.0)

    def calc_bpd_loop(self, model, x_start, clip_denoised=True, model_kwargs=None):
        """
        Compute the entire variational lower-bound, measured in bits-per-dim,
        as well as other related quantities.
        :param model: the model to evaluate loss on.
        :param x_start: the [N x C x ...] tensor of inputs.
        :param clip_denoised: if True, clip denoised samples.
        :param model_kwargs: if not None, a dict of extra keyword arguments to
            pass to the model. This can be used for conditioning.
        :return: a dict containing the following keys:
                 - total_bpd: the total variational lower-bound, per batch element.
                 - prior_bpd: the prior term in the lower-bound.
                 - vb: an [N x T] tensor of terms in the lower-bound.
                 - xstart_mse: an [N x T] tensor of x_0 MSEs for each timestep.
                 - mse: an [N x T] tensor of epsilon MSEs for each timestep.
        """
        device = x_start.device
        batch_size = x_start.shape[0]

        vb = []
        xstart_mse = []
        mse = []
        for t in list(range(self.num_timesteps))[::-1]:
            t_batch = th.tensor([t] * batch_size, device=device)
            noise = th.randn_like(x_start)
            x_t = self.q_sample(x_start=x_start, t=t_batch, noise=noise)

            # Calculate VLB term at the current timestep
            with th.no_grad():
                out = self._vb_terms_bptimestepsd(
                    model,
                    x_start=x_start,
                    x_t=x_t,
                    t=t_batch,
                    clip_denoised=clip_denoised,
                    model_kwargs=model_kwargs,
                )
            vb.append(out["output"])
            xstart_mse.append(mean_flat((out["pred_xstart"] - x_start) ** 2))
            eps = self._predict_eps_from_xstart(x_t, t_batch, out["pred_xstart"])
            mse.append(mean_flat((eps - noise) ** 2))

        vb = th.stack(vb, dim=1)
        xstart_mse = th.stack(xstart_mse, dim=1)
        mse = th.stack(mse, dim=1)

        prior_bpd = self._prior_bpd(x_start)
        total_bpd = vb.sum(dim=1) + prior_bpd
        return {
            "total_bpd": total_bpd,
            "prior_bpd": prior_bpd,
            "vb": vb,
            "xstart_mse": xstart_mse,
            "mse": mse,
        }


def _extract_into_tensor(arr, timesteps, broadcast_shape):
    """
    Extract values from a 1-D numpy array for a batch of indices.
    :param arr: the 1-D numpy array.
    :param timesteps: a tensor of indices into the array to extract.
    :param broadcast_shape: a larger shape of K dimensions with the batch
                            dimension equal to the length of timesteps.
    :return: a tensor of shape [batch_size, 1, ...] where the shape has K dims.
    """
    res = th.from_numpy(arr).to(device=timesteps.device)[timesteps].float()
    while len(res.shape) < len(broadcast_shape):
        res = res[..., None]
    return res.expand(broadcast_shape)
