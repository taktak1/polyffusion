import torch
import numpy as np
from os.path import join
from argparse import ArgumentParser
import pickle
from tqdm import tqdm
from datetime import datetime
from matplotlib import pyplot as plt

from params import params
from dataset import DataSampleNpz
from dirs import *
from utils import prmat2c_to_midi_file
from ddpm import DenoiseDiffusion
from ddpm.unet import UNet
from ddpm.utils import gather
from model import Diffpro_DDPM


class Configs():
    # U-Net model for $\textcolor{lightgreen}{\epsilon_\theta}(x_t, t)$
    eps_model: UNet
    # [DDPM algorithm](index.html)
    diffusion: DenoiseDiffusion

    # Adam optimizer
    optimizer: torch.optim.Adam

    def __init__(self, params, model_dir):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(self.device)
        self.eps_model = UNet(
            image_channels=params.image_channels,
            n_channels=params.n_channels,
            ch_mults=params.channel_multipliers,
            is_attn=params.is_attention,
        ).to(self.device)

        # Create [DDPM class](index.html)
        self.diffusion = DenoiseDiffusion(
            eps_model=self.eps_model,
            n_steps=params.n_steps,
            device=self.device,
        )

        self.model = Diffpro_DDPM.load_trained(self.diffusion, model_dir,
                                               params).to(self.device)

        # self.song_fn, self.pnotree, _ = choose_song_from_val_dl()

        self.image_size_h = params.image_size_h
        self.image_size_w = params.image_size_w
        self.image_channels = params.image_channels
        self.n_steps = params.n_steps
        # $\beta_t$
        self.beta = self.diffusion.beta
        # $\alpha_t$
        self.alpha = self.diffusion.alpha
        # $\bar\alpha_t$
        self.alpha_bar = self.diffusion.alpha_bar
        # $\bar\alpha_{t-1}$
        alpha_bar_tm1 = torch.cat([self.alpha_bar.new_ones((1, )), self.alpha_bar[:-1]])

        # $\tilde\beta_t$
        self.beta_tilde = self.beta * (1 - alpha_bar_tm1) / (1 - self.alpha_bar)
        # $$\frac{\sqrt{\bar\alpha_{t-1}}\beta_t}{1 - \bar\alpha_t}$$
        self.mu_tilde_coef1 = self.beta * (alpha_bar_tm1**0.5) / (1 - self.alpha_bar)
        # $$\frac{\sqrt{\alpha_t}(1 - \bar\alpha_{t-1}}{1-\bar\alpha_t}$$
        self.mu_tilde_coef2 = (self.alpha**
                               0.5) * (1 - alpha_bar_tm1) / (1 - self.alpha_bar)
        # $\sigma^2 = \beta$
        self.sigma2 = self.beta

    def _sample_x0(self, xt: torch.Tensor, n_steps: int):
        """
        #### Sample an image using $\textcolor{lightgreen}{p_\theta}(x_{t-1}|x_t)$

        * `xt` is $x_t$
        * `n_steps` is $t$
        """

        # Number of sampels
        n_samples = xt.shape[0]
        # Iterate until $t$ steps
        for t_ in tqdm(range(n_steps), desc="Sampling"):
            t = n_steps - t_ - 1
            # Sample from $\textcolor{lightgreen}{p_\theta}(x_{t-1}|x_t)$
            xt = self.model.p_sample(
                xt, xt.new_full((n_samples, ), t, dtype=torch.long)
            )
            if t_ % 100 == 0 or (t_ >= 900 and t_ % 25 == 0):
                self.show_image(xt, f"exp/x{t}.jpg")
                prmat_x = xt.squeeze().cpu().numpy()
                prmat2c_to_midi_file(prmat_x, f"exp/x{t + 1}.mid")

        # Return $x_0$
        return xt

    def sample(self, n_samples: int = 1, init_cond=None, init_step=None):
        """
        #### Generate images
        """
        # $x_T \sim p(x_T) = \mathcal{N}(x_T; \mathbf{0}, \mathbf{I})$
        if init_cond is not None:
            init_cond = init_cond.to(self.device)
            assert init_step is not None
            xt = self.model.q_sample(
                init_cond,
                init_cond.new_full((init_cond.shape[0], ), init_step, dtype=torch.long)
            )
        else:
            xt = torch.randn(
                [n_samples, self.image_channels, self.image_size_h, self.image_size_w],
                device=self.device
            )

        init_step = init_step or self.n_steps
        # $$x_0 \sim \textcolor{lightgreen}{p_\theta}(x_0|x_t)$$
        x0 = self._sample_x0(xt, init_step)

        return x0

    def p_sample(self, xt: torch.Tensor, t: torch.Tensor, eps_theta: torch.Tensor):
        """
        #### Sample from $\textcolor{lightgreen}{p_\theta}(x_{t-1}|x_t)$
        """
        # [gather](utils.html) $\bar\alpha_t$
        alpha_bar = gather(self.alpha_bar, t)
        # $\alpha_t$
        alpha = gather(self.alpha, t)
        # $\frac{\beta}{\sqrt{1-\bar\alpha_t}}$
        eps_coef = (1 - alpha) / (1 - alpha_bar)**.5
        # $$\frac{1}{\sqrt{\alpha_t}} \Big(x_t -
        #      \frac{\beta_t}{\sqrt{1-\bar\alpha_t}}\textcolor{lightgreen}{\epsilon_\theta}(x_t, t) \Big)$$
        mean = 1 / (alpha**0.5) * (xt - eps_coef * eps_theta)
        # $\sigma^2$
        var = gather(self.sigma2, t)

        # $\epsilon \sim \mathcal{N}(\mathbf{0}, \mathbf{I})$
        eps = torch.randn(xt.shape, device=xt.device)
        # Sample
        return mean + (var**.5) * eps

    def predict(self, n_samples: int = 16, init_cond=False, init_step=None):
        self.model.eval()
        with torch.no_grad():
            if not init_cond:
                x0 = self.sample(n_samples)
                self.show_image(x0, "exp/x0.jpg")
                prmat_x = x0.squeeze().cpu().numpy()
                output_stamp = f"ddpm_prmat2c_[uncond]_{datetime.now().strftime('%m-%d_%H%M%S')}"
                prmat2c_to_midi_file(prmat_x, f"exp/{output_stamp}.mid")
                return x0
            else:
                song_fn, x_init, _ = choose_song_from_val_dl()
                x0 = self.sample(n_samples, init_cond=x_init, init_step=init_step)
                self.show_image(x0, "exp/x0.jpg")
                prmat_x = x0.squeeze().cpu().numpy()
                output_stamp = f"ddpm_prmat2c_init_[{song_fn}]_{datetime.now().strftime('%m-%d_%H%M%S')}"
                prmat2c_to_midi_file(prmat_x, f"exp/{output_stamp}.mid")
                return x0

    def show_image(self, img, title=""):
        """Helper function to display an image"""
        # (B, 2, 32, 128)
        img = img.clip(0, 1)
        img = img.cpu().numpy()
        if img.ndim == 4:
            img = np.swapaxes(img, 1, 2)
            img = np.concatenate(img, axis=0)
            img = np.swapaxes(img, 0, 1)
        print(img.shape)
        h = img.shape[1]
        w = img.shape[2]
        img = np.append(img, np.zeros([1, h, w]), axis=0)
        img = img.transpose(2, 1, 0)  # (128, 32, 3)
        plt.imsave(title, img)


def choose_song_from_val_dl():
    split_fpath = join(TRAIN_SPLIT_DIR, "musicalion.pickle")
    with open(split_fpath, "rb") as f:
        split = pickle.load(f)
    print(split[1])
    num = int(input("choose one:"))
    song_fn = split[1][num]
    print(song_fn)

    song = DataSampleNpz(song_fn)
    prmat_x, _ = song.get_whole_song_data()
    prmat_x_np = prmat_x.squeeze().cpu().numpy()
    prmat2c_to_midi_file(prmat_x_np, "exp/origin_x.mid")
    return song_fn, prmat_x, prmat_x


if __name__ == "__main__":
    parser = ArgumentParser(description='inference a Diffpro model')
    parser.add_argument(
        "--model_dir", help='directory in which trained model checkpoints are stored'
    )
    args = parser.parse_args()
    config = Configs(params, args.model_dir)
    config.predict(n_samples=16, init_cond=False, init_step=100)
