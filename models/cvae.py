import torch, time, random, os, pickle
import numpy as np
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torchvision import datasets, transforms
from datasets import BodyMapDataset
from torch.utils.data import DataLoader
from torch.nn import init
from torch.optim import lr_scheduler
from cv2 import imread, imwrite, connectedComponents
from torchsummary import summary
from models.networks import *
from models import utils
from visdom import Visdom
from prefetch_generator import BackgroundGenerator

class DataLoaderX(DataLoader):
    
    def __iter__(self):
        return BackgroundGenerator(super().__iter__())

class CVAE(object):
    def __init__(self, args):
        # parameters
        self.epoch = args.epoch
        self.sample_num = 10
        self.batch_size = args.batch_size
        self.save_dir = args.save_dir
        self.result_dir = args.result_dir
        self.dataset = args.dataset
        self.device = args.device
        self.y_dim = args.y_dim
        self.z_dim = args.z_dim
        self.pix_dim = args.pix_dim
        
        # Load mask
        # self.mask = utils.generate_mask("stretch" in self.dataset, self.pix_dim, self.y_dim).to(self.device)
        self.mask = utils.generate_mask_guass("stretch" in self.dataset, self.pix_dim, self.y_dim).to(self.device)
        
        # Load datasets
        self.train_data = BodyMapDataset(data_root=args.data_dir, dataset=args.dataset, max_size=args.data_size, dim=args.pix_dim, cls_num=args.y_dim)

        self.sample_per_batch = len(self.train_data)/self.batch_size
        print("Trainset: %d \n"%(len(self.train_data)))

        self.trainset_loader = DataLoaderX(
            dataset=self.train_data,
            batch_size=args.batch_size,
            drop_last=True,
            shuffle=True,
            num_workers=args.worker,
            pin_memory=True
        )
        
        # networks init
        self.En = nn.DataParallel(encoder(args, self.dataset))
        self.De = nn.DataParallel(decoder(args, self.dataset))
        self.CVAE = CVAE_T(args, self.En, self.De)
        
        if args.resume:
            self.load()

        if args.testmode:
            self.load(args.pkl)         
            
        self.CVAE_optimizer = optim.AdamW(self.CVAE.parameters(), lr=args.lrG, betas=(0.5, 0.999), eps=1e-08)
        # self.CVAE_scheduler = torch.optim.lr_scheduler.StepLR(self.CVAE_optimizer, step_size=50, gamma=0.3)
        self.CVAE_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.CVAE_optimizer, mode='min', factor=0.3, patience=5, verbose=True, threshold=1e-4)
        
        # to device
        self.CVAE.to(self.device)
        self.L1_loss = nn.L1Loss().to(self.device)
        self.MSE_loss = nn.MSELoss().to(self.device)

        # fixed noise & condition
        self.sample_z_ = torch.zeros((self.sample_num * self.y_dim, self.z_dim))
        
        for i in range(self.sample_num):
            self.sample_z_[i * self.y_dim] = torch.from_numpy(utils.gaussian(1, self.z_dim, mean=random.uniform(-0.5,0.5), var=random.uniform(0.5,1.5)))
            for j in range(1, self.y_dim):
                self.sample_z_[i * self.y_dim + j] = self.sample_z_[i * self.y_dim]

        temp = torch.linspace(0, self.y_dim-1, self.y_dim).reshape(-1,1)
        temp_y = torch.zeros((self.sample_num * self.y_dim, 1))
        for i in range(self.sample_num):
            temp_y[i * self.y_dim: (i + 1) * self.y_dim] = temp

        self.sample_y_ = torch.zeros((self.sample_num* self.y_dim, self.y_dim))
        self.sample_y_.scatter_(1, temp_y.type(torch.LongTensor), 1)
        
        self.fill = torch.zeros((self.y_dim, self.y_dim, self.pix_dim, self.pix_dim))
        for i in range(self.y_dim):
            self.fill[i, i, :, :] = 1

    def train(self):
        self.train_hist = {}
        self.train_hist['VAE_loss'] = []
        self.train_hist['KL_loss'] = []
        self.train_hist['LL_loss'] = []
        self.train_hist['per_epoch_time'] = []
        self.train_hist['total_time'] = []

        self.CVAE.train()
        print('training start!!')
        
        # visdom
        viz = Visdom(port=2000, env=self.dataset)
        assert viz.check_connection()
        iter_plot_loc = utils.create_loc_plot(viz, 'Iteration', 'Loss', '%s:(iter)'%(self.dataset), ['VAE', 'KL', 'LL'])
        epoch_plot_loc = utils.create_loc_plot(viz, 'Epoch', 'Loss', '%s:(epoch)'%(self.dataset), ['VAE', 'KL', 'LL'])
        iter_plot_vis = utils.create_vis_plot(viz, 'Iter-Visualization', self.batch_size, self.pix_dim)
    
        start_time = time.time()
        for epoch in range(self.epoch):
            
            epoch_start_time = time.time()
            VAE_loss_total = []
            KL_loss_total = []
            LL_loss_total = []
            
            self.En.train()
            self.De.train()
            
            for iter, (imgs, labels) in enumerate(self.trainset_loader):
                
                x_ = imgs.to(self.device)
                y_vec_ = labels.to(self.device)
                y_fill_ = self.fill[torch.max(y_vec_, 1)[1].squeeze()].to(self.device)
                             
                # update VAE network
                dec = self.CVAE(x_, y_fill_, y_vec_)
                self.CVAE_optimizer.zero_grad()

                label_mask = torch.unsqueeze(self.mask[(labels==1.0).nonzero()[:,1]],1)

                KL_loss = latent_loss(self.CVAE.z_mean, self.CVAE.z_sigma)/(self.batch_size)
                # LL_loss = self.MSE_loss(dec*label_mask, x_*label_mask)/(self.batch_size)
                LL_loss = self.MSE_loss(dec, x_)/(self.batch_size)*50.0
                VAE_loss = LL_loss + KL_loss

                # print(dec.shape, x_.shape)
                
                VAE_loss_total.append(VAE_loss.item())
                KL_loss_total.append(KL_loss.item())
                LL_loss_total.append(LL_loss.item())

                self.train_hist['VAE_loss'].append(VAE_loss.item())
                self.train_hist['KL_loss'].append(KL_loss.item())
                self.train_hist['LL_loss'].append(LL_loss.item())

                VAE_loss.backward()

                # gradient clipping
                # torch.nn.utils.clip_grad_norm_(self.CVAE.parameters(), 5)

                self.CVAE_optimizer.step()

                if ((iter + 1) % (3)) == 0:
                    print("Epoch: [%2d] [%4d/%4d] time: %4.4f VAE_loss: %.8f KL_loss: %.8f LL_loss: %.8f" %
                          ((epoch + 1), (iter + 1), len(self.train_data) // self.batch_size, time.time() - start_time,
                           VAE_loss.item(), KL_loss.item(), LL_loss.item()))
                    utils.update_loc_plot(viz, iter_plot_loc, "iter", epoch, iter, self.sample_per_batch, [VAE_loss.item(), KL_loss.item(), LL_loss.item()])
                    utils.update_vis_plot(viz, iter_plot_vis, self.batch_size, dec, x_)
                    
            # test samples
            self.En.eval()
            self.De.eval()
            
            with torch.no_grad():
                samples = self.De(self.sample_z_.to(self.device), self.sample_y_.to(self.device))
            samples = samples.detach().cpu().numpy().transpose(0, 2, 3, 1)
            tot_num_samples = self.sample_num * self.y_dim
            manifold_h = self.sample_num
            manifold_w = self.y_dim
            utils.save_images(samples[:manifold_h * manifold_w, :, :, :], [manifold_h, manifold_w], self.pix_dim,
                        utils.check_folder(self.result_dir + '/' + self.model_dir) + '/' + self.dataset +
                        '_train_{:02d}_{:04d}.png'.format(epoch, (iter + 1)))
            
            if epoch % 10 == 0:
                self.save()
            self.train_hist['per_epoch_time'].append(time.time() - epoch_start_time)
            utils.update_loc_plot(viz, epoch_plot_loc, "epoch", epoch, iter, self.sample_per_batch, [VAE_loss_total, KL_loss_total, LL_loss_total])

        self.train_hist['total_time'].append(time.time() - start_time)
        print("Avg one epoch time: %.2f, total %d epochs time: %.2f" % (np.mean(self.train_hist['per_epoch_time']),
              self.epoch, self.train_hist['total_time'][0]))
        print("Training finish!... save training results")

        self.save()
        utils.generate_train_animation(self.result_dir + '/'+ self.model_dir + '/'+ self.dataset, self.epoch, len(self.train_data)/self.batch_size)
        utils.loss_VAE_plot(self.train_hist, os.path.join(self.save_dir, self.model_dir), self.dataset)

        # change the lr
        self.CVAE_scheduler.step(torch.mean(torch.FloatTensor(VAE_loss_total)))

    def test(self):
                    
        # test samples
        self.En.eval()
        self.De.eval()
        
        with torch.no_grad():

            iter_num = 10
            outs = []
            ins = []
            for iter, (imgs, labels) in enumerate(self.trainset_loader):
                x_ = imgs.to(self.device)
                y_vec_ = labels.to(self.device)
                y_fill_ = self.fill[torch.max(y_vec_, 1)[1].squeeze()].to(self.device)
                out = self.CVAE(x_, y_fill_, y_vec_)
                out = out.detach().cpu().numpy().transpose(0, 2, 3, 1)
                x_ = x_.detach().cpu().numpy().transpose(0, 2, 3, 1)
                outs.append(out)
                ins.append(x_)

            utils.save_images_test(ins, outs, iter_num, self.batch_size, [self.pix_dim, self.pix_dim],
                        utils.check_folder(self.result_dir + '/' + self.model_dir) + '/' + self.dataset +
                        '_test_{:03d}.png'.format(iter_num))


    @property
    def model_dir(self):
        return "VAE_data_{}_pix_{}_batch_{}_embed_{}".format(
            self.dataset, self.pix_dim, self.batch_size, self.z_dim)

    def save(self):
        save_dir = os.path.join(self.save_dir, self.model_dir)

        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        torch.save(self.En.module.state_dict(), os.path.join(save_dir, self.dataset + '_encoder.pkl'))
        torch.save(self.De.module.state_dict(), os.path.join(save_dir, self.dataset + '_decoder.pkl'))

        with open(os.path.join(save_dir, self.dataset + '_history.pkl'), 'wb') as f:
            pickle.dump(self.train_hist, f)

    def load(self, pkl=None):
        if pkl is None:
            save_dir = os.path.join(self.save_dir, self.model_dir)
            self.En.module.load_state_dict(torch.load(os.path.join(save_dir, self.dataset + '_encoder.pkl')))
            self.De.module.load_state_dict(torch.load(os.path.join(save_dir, self.dataset + '_decoder.pkl')))
        else:
            self.En.module.load_state_dict(torch.load(pkl + '_encoder.pkl'))
            self.De.module.load_state_dict(torch.load(pkl + '_decoder.pkl'))

