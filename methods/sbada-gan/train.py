import sys
from itertools import chain

import click
import os
import torch
import torch.cuda
from tensorboardX import SummaryWriter
from torch.nn import functional as F
from torch.optim import Adam
from torch.utils.data import DataLoader
from torchvision.utils import make_grid

sys.path.append('../..')

from util.dataset import DADataset, load_source_target_datasets
from util.loss import GANLoss
from util.net import weights_init, Discriminator, Generator, LenetClassifier
from util.opt import exp_list
from util.preprocess import get_composed_transforms
from util.image_pool import ImagePool
from util.sampler import InfiniteSampler
from util.io import save_models_dict, get_config

torch.backends.cudnn.benchmark = True


@click.command()
@click.option('--exp', type=click.Choice(exp_list), required=True)
@click.option('--affine', is_flag=True)
@click.option('--num_epochs', type=int, default=200)
def experiment(exp, affine, num_epochs):
    writer = SummaryWriter()
    log_dir = 'log/{:s}/sbada'.format(exp)
    os.makedirs(log_dir, exist_ok=True)
    device = torch.device('cuda')

    config = get_config('config.yaml')

    alpha = float(config['weight']['alpha'])
    beta = float(config['weight']['beta'])
    gamma = float(config['weight']['gamma'])
    mu = float(config['weight']['mu'])
    new = float(config['weight']['new'])
    eta = 0.0
    batch_size = int(config['batch_size'])
    pool_size = int(config['pool_size'])
    lr = float(config['lr'])
    weight_decay = float(config['weight_decay'])

    src, tgt = load_source_target_datasets(exp)

    n_ch_s = src.train_X.shape[1]  # number of color channels
    n_ch_t = tgt.train_X.shape[1]  # number of color channels
    res = src.train_X.shape[-1]  # size of image
    n_classes = src.n_classes

    train_tfs = get_composed_transforms(train=True, hflip=False)
    test_tfs = get_composed_transforms(train=False, hflip=False)

    src_train = DADataset(src.train_X, src.train_y, train_tfs, affine)
    tgt_train = DADataset(tgt.train_X, None, train_tfs, affine)
    tgt_test = DADataset(tgt.test_X, tgt.test_y, test_tfs, affine)
    del src, tgt

    n_sample = max(len(src_train), len(tgt_train))
    iter_per_epoch = n_sample // batch_size + 1

    weights_init_kaiming = weights_init('kaiming')
    weights_init_gaussian = weights_init('gaussian')

    cls_s = LenetClassifier(n_classes, n_ch_s, res).to(device)
    cls_t = LenetClassifier(n_classes, n_ch_t, res).to(device)

    cls_s.apply(weights_init_kaiming)
    cls_t.apply(weights_init_kaiming)

    gen_s_t_params = {'res': res, 'n_c_in': n_ch_s, 'n_c_out': n_ch_t}
    gen_t_s_params = {'res': res, 'n_c_in': n_ch_t, 'n_c_out': n_ch_s}
    gen_s_t = Generator(**{**config['gen_init'], **gen_s_t_params}).to(device)
    gen_t_s = Generator(**{**config['gen_init'], **gen_t_s_params}).to(device)
    gen_s_t.apply(weights_init_gaussian)
    gen_t_s.apply(weights_init_gaussian)

    dis_s_params = {'res': res, 'n_c_in': n_ch_s}
    dis_t_params = {'res': res, 'n_c_in': n_ch_t}
    dis_s = Discriminator(**{**config['dis_init'], **dis_s_params}).to(device)
    dis_t = Discriminator(**{**config['dis_init'], **dis_t_params}).to(device)
    dis_s.apply(weights_init_gaussian)
    dis_t.apply(weights_init_gaussian)

    config = {'lr': lr, 'weight_decay': weight_decay, 'betas': (0.5, 0.999)}
    opt_gen = Adam(
        chain(gen_s_t.parameters(), gen_t_s.parameters(),
              cls_s.parameters(), cls_t.parameters()), **config)
    opt_dis = Adam(chain(dis_s.parameters(), dis_t.parameters()), **config)

    calc_ls = GANLoss(device, use_lsgan=True)
    calc_ce = F.cross_entropy

    fake_src_x_pool = ImagePool(pool_size * batch_size)
    fake_tgt_x_pool = ImagePool(pool_size * batch_size)

    src_train_iter = iter(DataLoader(
        src_train, batch_size=batch_size, num_workers=4,
        sampler=InfiniteSampler(len(src_train))))
    tgt_train_iter = iter(DataLoader(
        tgt_train, batch_size=batch_size, num_workers=4,
        sampler=InfiniteSampler(len(tgt_train))))
    tgt_test_loader = DataLoader(
        tgt_test, batch_size=batch_size * 4, num_workers=4)
    print('Training...')

    cls_s.train()
    cls_t.train()

    niter = 0
    while True:
        niter += 1
        src_x, src_y = next(src_train_iter)
        tgt_x = next(tgt_train_iter)
        src_x, src_y = src_x.to(device), src_y.to(device)
        tgt_x = tgt_x.to(device)

        if niter >= num_epochs * 0.75 * iter_per_epoch:
            eta = config['weight']['eta']

        fake_tgt_x = gen_s_t(src_x)
        fake_back_src_x = gen_t_s(fake_tgt_x)
        fake_src_x = gen_t_s(tgt_x)

        with torch.no_grad():
            fake_src_pseudo_y = torch.max(cls_s(fake_src_x), dim=1)[1]

        # eq2
        loss_gen = beta * calc_ce(cls_t(fake_tgt_x), src_y)
        loss_gen += mu * calc_ce(cls_s(src_x), src_y)

        # eq3
        loss_gen += gamma * calc_ls(dis_s(fake_src_x), True)
        loss_gen += alpha * calc_ls(dis_t(fake_tgt_x), True)

        # eq5
        loss_gen += eta * calc_ce(cls_s(fake_src_x), fake_src_pseudo_y)

        # eq6
        loss_gen += new * calc_ce(cls_s(fake_back_src_x), src_y)

        # do not backpropagate loss to generator
        fake_tgt_x = fake_tgt_x.detach()
        fake_src_x = fake_src_x.detach()
        fake_back_src_x = fake_back_src_x.detach()

        # eq3
        loss_dis_s = gamma * calc_ls(
            dis_s(fake_src_x_pool.query(fake_src_x)), False)
        loss_dis_s += gamma * calc_ls(dis_s(src_x), True)
        loss_dis_t = alpha * calc_ls(
            dis_t(fake_tgt_x_pool.query(fake_tgt_x)), False)
        loss_dis_t += alpha * calc_ls(dis_t(tgt_x), True)

        loss_dis = loss_dis_s + loss_dis_t

        for opt, loss in zip([opt_dis, opt_gen], [loss_dis, loss_gen]):
            opt.zero_grad()
            loss.backward(retain_graph=True)
            opt.step()

        if niter % 100 == 0 and niter > 0:
            writer.add_scalar('dis/src', loss_dis_s.item(), niter)
            writer.add_scalar('dis/tgt', loss_dis_t.item(), niter)
            writer.add_scalar('gen', loss_gen.item(), niter)

        if niter % iter_per_epoch == 0:
            epoch = niter // iter_per_epoch

            if epoch % 10 == 0:
                data = []
                for x in [src_x, fake_tgt_x, fake_back_src_x, tgt_x,
                          fake_src_x]:
                    x = x.to(torch.device('cpu'))
                    if x.size(1) == 1:
                        x = x.repeat(1, 3, 1, 1)  # grayscale2rgb
                    data.append(x)
                grid = make_grid(torch.cat(tuple(data), dim=0),
                                 normalize=True, range=(-1.0, 1.0))
                writer.add_image('generated', grid, epoch)

            cls_t.eval()

            n_err = 0
            with torch.no_grad():
                for tgt_x, tgt_y in tgt_test_loader:
                    prob_y = F.softmax(cls_t(tgt_x.to(device)), dim=1)
                    pred_y = torch.max(prob_y, dim=1)[1]
                    pred_y = pred_y.to(torch.device('cpu'))
                    n_err += (pred_y != tgt_y).sum().item()

            writer.add_scalar('err_tgt', n_err / len(tgt_test), epoch)

            cls_t.train()

            if epoch % 50 == 0:
                models_dict = {
                    'cls_s': cls_s, 'cls_t': cls_t, 'dis_s': dis_s,
                    'dis_t': dis_t, 'gen_s_t': gen_s_t, 'gen_t_s': gen_t_s}
                filename = '{:s}/epoch{:d}.tar'.format(log_dir, epoch)
                save_models_dict(models_dict, filename)

            if epoch >= num_epochs:
                break


if __name__ == '__main__':
    experiment()
