import numpy as np
import pandas as pd
import random
from tqdm import tqdm

from scipy.ndimage import gaussian_filter1d

import matplotlib.pyplot as plt

from matplotlib import cm
from matplotlib.animation import FuncAnimation
from mpl_toolkits.mplot3d import Axes3D

import torch
import torch.nn as nn
import torch.nn.functional as Fn
import pytorch_lightning as lightning

from pytorch_lightning.loggers import CSVLogger
from pytorch_lightning.callbacks import ModelCheckpoint, GradientAccumulationScheduler
from pytorch_lightning.strategies import DeepSpeedStrategy
from pytorch_lightning.strategies import DDPStrategy
from pytorch_lightning.plugins.precision import deepspeed

from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error

import xfads.utils as utils
import xfads.prob_utils as prob_utils

from xfads import plot_utils

from xfads.ssm_modules.likelihoods import GaussianLikelihood
from xfads.ssm_modules.dynamics import DenseGaussianDynamics
from xfads.ssm_modules.dynamics import DenseGaussianInitialCondition
from xfads.ssm_modules.encoders import LocalEncoderLRMvn, BackwardEncoderLRMvn
from xfads.smoothers.lightning_trainers import LightningNonlinearSSM, LightningMonkeyReaching
from xfads.smoothers.nonlinear_smoother_causal import NonlinearFilter, LowRankNonlinearStateSpaceModel



if torch.cuda.is_available():
    import os
    # To avoid GPU Memory Fragmentation
    os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'


class Cfg(dict):
    def __getattr__(self, attr):
        if attr in self:
            return self[attr]
        else:
            raise AttributeError(f"'DictAsAttributes' object has no attribute '{attr}'")


def main():
    
    """config"""
    cfg = {
        # --- graphical model --- #
        'n_latents': 40,
        'n_latents_read': 35,

        'rank_local': 15,
        'rank_backward': 5,

        'n_hidden_dynamics': 128,

        # --- inference network --- #
        'n_samples': 25,
        'n_hidden_local': 256,
        'n_hidden_backward': 128,

        # --- hyperparameters --- #
        'use_cd': False,
        'p_mask_a': 0.0,
        'p_mask_b': 0.0,
        'p_mask_apb': 0.0,
        'p_mask_y_in': 0.0,
        'p_local_dropout': 0.4,
        'p_backward_dropout': 0.0,

        # --- training --- #
        'device': 'cuda',
        'data_device': 'cuda',

        'lr': 1e-3,
        'n_epochs': 1000,
        'batch_sz': 128,
        'minibatch_sz': 8,
        'use_minibatching': False,

        # --- misc --- #
        'bin_sz': 20e-3,
        'bin_sz_ms': 20,

        'n_bins_enc': 45,
        'n_bins_bhv': 10,

        # --- smoothing spike trains --- #
        'gaussian_kernel_ms': 80,

        'seed': 1236,
        'default_dtype': torch.float32,

        'shuffle_train': True,
        'shuffle_valid': False,
        'shuffle_test': False,

        # --- ray --- #
        'n_ray_samples': 10,
    }
    cfg = Cfg(cfg)
    
    lightning.seed_everything(cfg.seed, workers=True)
    torch.set_default_dtype(torch.float32)


    """load the data"""
    data_path = '/home/makki/data/data_{split}_{bin_size_ms}ms.pt'
    train_data = torch.load(data_path.format(split='train', bin_size_ms=cfg.bin_sz_ms))
    val_data = torch.load(data_path.format(split='valid', bin_size_ms=cfg.bin_sz_ms))
    test_data = torch.load(data_path.format(split='test', bin_size_ms=cfg.bin_sz_ms))

    y_train_obs = train_data['y_obs'].type(torch.float32).to(cfg.data_device)[:, :cfg.n_bins_enc, :]
    y_valid_obs = val_data['y_obs'].type(torch.float32).to(cfg.data_device)[:, :cfg.n_bins_enc, :]
    y_test_obs = test_data['y_obs'].type(torch.float32).to(cfg.data_device)[:, :cfg.n_bins_enc, :]

    vel_train = torch.stack((train_data['cursor_vel_x'], train_data['cursor_vel_y']), dim=-1)[:, :cfg.n_bins_enc, :]
    vel_valid = torch.stack((val_data['cursor_vel_x'], val_data['cursor_vel_y']), dim=-1)[:, :cfg.n_bins_enc, :]
    vel_test = torch.stack((test_data['cursor_vel_x'], test_data['cursor_vel_y']), dim=-1)[:, :cfg.n_bins_enc, :]

    y_train_dataset = torch.utils.data.TensorDataset(y_train_obs, vel_train)
    y_val_dataset = torch.utils.data.TensorDataset(y_valid_obs, vel_valid)
    y_test_dataset = torch.utils.data.TensorDataset(y_test_obs, vel_test)

    train_dataloader = torch.utils.data.DataLoader(y_train_dataset, batch_size=cfg.batch_sz, shuffle=True)
    valid_dataloader = torch.utils.data.DataLoader(y_val_dataset, batch_size=y_valid_obs.shape[0], shuffle=False)
    test_dataloader = torch.utils.data.DataLoader(y_test_dataset, batch_size=y_valid_obs.shape[0], shuffle=False)

    n_train_trials, n_bins, n_neurons_obs = y_train_obs.shape
    n_valid_trials = y_valid_obs.shape[0]
    n_test_trials = y_test_obs.shape[0]

    cfg['n_bins'] = n_bins
    cfg['n_bins_bhv'] = 10

    cfg['n_neurons_enc'] = n_neurons_obs
    cfg['n_neurons_obs'] = n_neurons_obs

    cfg = Cfg(cfg)

    """Gaussian-smoothed spike trains"""
    y_train_obs = torch.tensor(
        gaussian_filter1d(y_train_obs.cpu(), sigma=cfg.gaussian_kernel_ms//cfg.bin_sz_ms, axis=1)
    ).to(cfg.data_device)
    y_valid_obs = torch.tensor(
        gaussian_filter1d(y_valid_obs.cpu(), sigma=cfg.gaussian_kernel_ms//cfg.bin_sz_ms, axis=1)
    ).to(cfg.data_device)
    y_test_obs = torch.tensor(
        gaussian_filter1d(y_test_obs.cpu(), sigma=cfg.gaussian_kernel_ms//cfg.bin_sz_ms, axis=1)
    ).to(cfg.data_device)

#####################################################################################################

    """likelihood pdf"""
    R_diag = torch.ones(n_neurons_obs, device=cfg.device)
    H = utils.ReadoutLatentMask(cfg.n_latents, cfg.n_latents_read)
    readout_fn = nn.Sequential(H, nn.Linear(cfg.n_latents_read, n_neurons_obs))
    likelihood_pdf = GaussianLikelihood(readout_fn, n_neurons_obs, device=cfg.device)

    """dynamics module"""
    Q_diag = 1. * torch.ones(cfg.n_latents, device=cfg.device)
    dynamics_fn = utils.build_gru_dynamics_function(cfg.n_latents, cfg.n_hidden_dynamics, device=cfg.device)
    dynamics_mod = DenseGaussianDynamics(dynamics_fn, cfg.n_latents, Q_diag, device=cfg.device)

    """initial condition"""
    m_0 = torch.zeros(cfg.n_latents, device=cfg.device)
    Q_0_diag = 1. * torch.ones(cfg.n_latents, device=cfg.device)
    initial_condition_pdf = DenseGaussianInitialCondition(cfg.n_latents, m_0, Q_0_diag, device=cfg.device)

    """local/backward encoder"""
    backward_encoder = BackwardEncoderLRMvn(cfg.n_latents, cfg.n_hidden_backward, cfg.n_latents,
                                            rank_local=cfg.rank_local, rank_backward=cfg.rank_backward,
                                            device=cfg.device)
    local_encoder = LocalEncoderLRMvn(cfg.n_latents, n_neurons_obs, cfg.n_hidden_local, cfg.n_latents,
                                      rank=cfg.rank_local,
                                      device=cfg.device, dropout=cfg.p_local_dropout)
    nl_filter = NonlinearFilter(dynamics_mod, initial_condition_pdf, device=cfg.device)

    """sequence vae"""
    ssm = LowRankNonlinearStateSpaceModel(dynamics_mod, likelihood_pdf, initial_condition_pdf, backward_encoder,
                                          local_encoder, nl_filter, device=cfg.device)
    
    """lightning"""
    #seq_vae = LightningMonkeyReaching(ssm, cfg, cfg.n_bins_enc, cfg.n_bins_bhv)
    seq_vae = LightningNonlinearSSM(ssm, cfg)

    #model_ckpt_path = 'ckpts/epoch=401_valid_loss=15941.9287109375.ckpt'
    #seq_vae = LightningNonlinearSSM.load_from_checkpoint(model_ckpt_path, ssm=ssm, cfg=cfg,
    #                                                       n_time_bins_enc=cfg.n_bins_enc, n_time_bins_bhv=cfg.n_bins_bhv, strict=False)

    csv_logger = CSVLogger('logs/smoother/acausal/',
                           name=f'sd_{cfg.seed}_r_y_{cfg.rank_local}_r_b_{cfg.rank_backward}',
                           version='smoother_acausal')
    ckpt_callback = ModelCheckpoint(save_top_k=3, monitor='valid_loss', mode='max',
                                    dirpath='ckpts/smoother/acausal/', save_last=True,
                                    filename='{epoch:0}_{valid_loss:0.2f}_{r2_valid_enc:0.2f}_{r2_valid_bhv:0.2f}_{valid_bps_enc:0.2f}')

    trainer = lightning.Trainer(max_epochs=cfg.n_epochs,
                                gradient_clip_val=1.0,
                                default_root_dir='lightning/',
                                callbacks=[ckpt_callback],
                                logger=csv_logger,
                                )

    trainer.fit(model=seq_vae, train_dataloaders=train_dataloader, val_dataloaders=valid_dataloader)
    torch.save(ckpt_callback.best_model_path, 'ckpts/smoother/acausal/best_model_path_1.ckpt')
    trainer.test(dataloaders=test_dataloader, ckpt_path='last-v3')

    #####################################################################################################

    cfg.n_epoch = 500
    cfg.p_mask_a = 0.2
    cfg = Cfg(cfg)

    """likelihood pdf"""
    R_diag = torch.ones(n_neurons_obs, device=cfg.device)
    H = utils.ReadoutLatentMask(cfg.n_latents, cfg.n_latents_read)
    readout_fn = nn.Sequential(H, nn.Linear(cfg.n_latents_read, n_neurons_obs))
    likelihood_pdf = GaussianLikelihood(readout_fn, n_neurons_obs, device=cfg.device)

    """dynamics module"""
    Q_diag = 1. * torch.ones(cfg.n_latents, device=cfg.device)
    dynamics_fn = utils.build_gru_dynamics_function(cfg.n_latents, cfg.n_hidden_dynamics, device=cfg.device)
    dynamics_mod = DenseGaussianDynamics(dynamics_fn, cfg.n_latents, Q_diag, device=cfg.device)

    """initial condition"""
    m_0 = torch.zeros(cfg.n_latents, device=cfg.device)
    Q_0_diag = 1. * torch.ones(cfg.n_latents, device=cfg.device)
    initial_condition_pdf = DenseGaussianInitialCondition(cfg.n_latents, m_0, Q_0_diag, device=cfg.device)

    """local/backward encoder"""
    backward_encoder = BackwardEncoderLRMvn(cfg.n_latents, cfg.n_hidden_backward, cfg.n_latents,
                                            rank_local=cfg.rank_local, rank_backward=cfg.rank_backward,
                                            device=cfg.device)
    local_encoder = LocalEncoderLRMvn(cfg.n_latents, n_neurons_obs, cfg.n_hidden_local, cfg.n_latents,
                                      rank=cfg.rank_local,
                                      device=cfg.device, dropout=cfg.p_local_dropout)
    nl_filter = NonlinearFilter(dynamics_mod, initial_condition_pdf, device=cfg.device)

    """sequence vae"""
    ssm = LowRankNonlinearStateSpaceModel(dynamics_mod, likelihood_pdf, initial_condition_pdf, backward_encoder,
                                          local_encoder, nl_filter, device=cfg.device)

    """lightning"""
    # seq_vae = LightningMonkeyReaching(ssm, cfg, cfg.n_bins_enc, cfg.n_bins_bhv)
    seq_vae = LightningNonlinearSSM(ssm, cfg)

    model_ckpt_path = 'ckpts/smoother/acausal/last-v3.ckpt'
    seq_vae = LightningNonlinearSSM.load_from_checkpoint(model_ckpt_path, ssm=ssm, cfg=cfg,
                                                         n_time_bins_enc=cfg.n_bins_enc, n_time_bins_bhv=cfg.n_bins_bhv,
                                                         strict=False)

    csv_logger = CSVLogger('logs/smoother/acausal/',
                           name=f'sd_{cfg.seed}_r_y_{cfg.rank_local}_r_b_{cfg.rank_backward}',
                           version='smoother_acausal')
    ckpt_callback = ModelCheckpoint(save_top_k=3, monitor='valid_loss', mode='max',
                                    dirpath='ckpts/smoother/acausal/', save_last=True,
                                    filename='{epoch:0}_{valid_loss:0.2f}_{r2_valid_enc:0.2f}_{r2_valid_bhv:0.2f}_{valid_bps_enc:0.2f}')

    trainer = lightning.Trainer(max_epochs=cfg.n_epochs,
                                gradient_clip_val=1.0,
                                default_root_dir='lightning/',
                                callbacks=[ckpt_callback],
                                logger=csv_logger,
                                )

    trainer.fit(model=seq_vae, train_dataloaders=train_dataloader, val_dataloaders=valid_dataloader)
    torch.save(ckpt_callback.best_model_path, 'ckpts/smoother/acausal/best_model_path_2.ckpt')
    trainer.test(dataloaders=test_dataloader, ckpt_path='last-v4')
bran
    ######################################################################################################

    cfg.n_epoch = 500
    cfg.p_mask_a = 0.4
    cfg = Cfg(cfg)

    """likelihood pdf"""
    R_diag = torch.ones(n_neurons_obs, device=cfg.device)
    H = utils.ReadoutLatentMask(cfg.n_latents, cfg.n_latents_read)
    readout_fn = nn.Sequential(H, nn.Linear(cfg.n_latents_read, n_neurons_obs))
    likelihood_pdf = GaussianLikelihood(readout_fn, n_neurons_obs, cfg.bin_sz, device=cfg.device)

    """dynamics module"""
    Q_diag = 1. * torch.ones(cfg.n_latents, device=cfg.device)
    dynamics_fn = utils.build_gru_dynamics_function(cfg.n_latents, cfg.n_hidden_dynamics, device=cfg.device)
    dynamics_mod = DenseGaussianDynamics(dynamics_fn, cfg.n_latents, Q_diag, device=cfg.device)

    """initial condition"""
    m_0 = torch.zeros(cfg.n_latents, device=cfg.device)
    Q_0_diag = 1. * torch.ones(cfg.n_latents, device=cfg.device)
    initial_condition_pdf = DenseGaussianInitialCondition(cfg.n_latents, m_0, Q_0_diag, device=cfg.device)

    """local/backward encoder"""
    backward_encoder = BackwardEncoderLRMvn(cfg.n_latents, cfg.n_hidden_backward, cfg.n_latents,
                                            rank_local=cfg.rank_local, rank_backward=cfg.rank_backward,
                                            device=cfg.device)
    local_encoder = LocalEncoderLRMvn(cfg.n_latents, n_neurons_obs, cfg.n_hidden_local, cfg.n_latents,
                                      rank=cfg.rank_local,
                                      device=cfg.device, dropout=cfg.p_local_dropout)
    nl_filter = NonlinearFilter(dynamics_mod, initial_condition_pdf, device=cfg.device)

    """sequence vae"""
    ssm = LowRankNonlinearStateSpaceModel(dynamics_mod, likelihood_pdf, initial_condition_pdf, backward_encoder,
                                          local_encoder, nl_filter, device=cfg.device)

    """lightning"""
    # seq_vae = LightningMonkeyReaching(ssm, cfg, cfg.n_bins_enc, cfg.n_bins_bhv)
    seq_vae = LightningNonlinearSSM(ssm, cfg)

    model_ckpt_path = 'ckpts/smoother/acausal/last-v4.ckpt'
    seq_vae = LightningNonlinearSSM.load_from_checkpoint(model_ckpt_path, ssm=ssm, cfg=cfg,
                                                         n_time_bins_enc=cfg.n_bins_enc, n_time_bins_bhv=cfg.n_bins_bhv, strict=False)

    csv_logger = CSVLogger('logs/smoother/acausal/',
                           name=f'sd_{cfg.seed}_r_y_{cfg.rank_local}_r_b_{cfg.rank_backward}',
                           version='smoother_acausal')
    ckpt_callback = ModelCheckpoint(save_top_k=3, monitor='valid_loss', mode='max',
                                    dirpath='ckpts/smoother/acausal/', save_last=True,
                                    filename='{epoch:0}_{valid_loss:0.2f}_{r2_valid_enc:0.2f}_{r2_valid_bhv:0.2f}_{valid_bps_enc:0.2f}')

    trainer = lightning.Trainer(max_epochs=cfg.n_epochs,
                                gradient_clip_val=1.0,
                                default_root_dir='lightning/',
                                callbacks=[ckpt_callback],
                                logger=csv_logger,
                                )

    trainer.fit(model=seq_vae, train_dataloaders=train_dataloader, val_dataloaders=valid_dataloader)
    torch.save(ckpt_callback.best_model_path, 'ckpts/smoother/acausal/best_model_path.pt')
    trainer.test(dataloaders=test_dataloader, ckpt_path='last-v5')


if __name__ == '__main__':
    main()
