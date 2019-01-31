import numpy as np
from torch.utils.data import Dataset, DataLoader
import pickle
import json
import glob
from tensorboardX import SummaryWriter
import random
import timeit
import torch
import torch.nn as nn
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
import torch.nn.functional as F
from ahoproc_tools.io import read_aco_file
import os
from waveminionet.models.frontend import WaveFe
from waveminionet.models.modules import Model
import librosa
from random import shuffle
import argparse
from utils import *


# Make Linear classifier model
class LinearClassifier(Model):
    
    def __init__(self, frontend,
                 num_spks=None, 
                 ft_fe=False,
                 z_bnorm=False,
                 name='CLS'):
        super().__init__(name=name)
        self.frontend = frontend
        self.ft_fe = ft_fe
        if z_bnorm:
            # apply z-norm to the input
            self.z_bnorm = nn.BatchNorm1d(frontend.emb_dim, affine=False)
        if num_spks is None:
            raise ValueError('Please specify a number of spks.')
        self.fc = nn.Conv1d(frontend.emb_dim, num_spks, 1)
        self.act = nn.LogSoftmax(dim=1)
    
    def forward(self, x):
        h = self.frontend(x)
        if not self.ft_fe:
            h = h.detach()
        if hasattr(self, 'z_bnorm'):
            h = self.z_bnorm(h)
        h = self.fc(h)
        y = self.act(h)
        return y

class LibriSpkIDDataset(Dataset):
    
    def __init__(self, data_root, files_list, spk2idx):
        super().__init__()
        self.files_list = files_list
        self.data_root = data_root
        self.spk2idx = spk2idx
    
    def __getitem__(self, idx):
        fpath = os.path.join(self.data_root, self.files_list[idx])
        wav, sr = librosa.load(fpath, sr=None)
        lab = self.spk2idx[self.files_list[idx]]
        return wav, lab

    def __len__(self):
        return len(self.files_list)
    
    
class WavCollater(object):
    
    def __init__(self, max_len=None):
        self.max_len = max_len
        
    def __call__(self, batch):
        if self.max_len is None:
            # track max seq len in batch
            # and apply it padding others seqs
            max_len = 0
            for sample in batch:
                wav, lab = sample
                clen = len(wav)
                if clen > max_len:
                    max_len = clen
        else:
            max_len = self.max_len
        X = []
        Y = []
        slens = []
        for sample in batch:
            wav, lab = sample
            clen = len(wav)
            if clen < max_len:
                # pad with zeros in the end
                P = max_len - clen
                pad = np.zeros((P,))
                wav = np.concatenate((wav, pad), axis=0)
            elif clen > max_len:
                # trim the end (applied if we specify max_len externally)
                idxs = list(range(clen - max_len))
                bidx = random.choice(idxs)
                wav = wav[bidx:bidx + max_len]
            X.append(wav)
            Y.append(lab)
            slens.append(clen)
        X = torch.FloatTensor(X)
        Y = torch.LongTensor(Y)
        slens = torch.LongTensor(slens)
        return X, Y, slens


class MLPClassifier(Model):

    def __init__(self, frontend,
                 num_spks=None,
                 ft_fe=False,
                 hidden_size=2048,
                 z_bnorm=False,
                 name='MLP'):
        # 2048 default size raises 5.6M params
        super().__init__(name=name)
        self.frontend = frontend
        self.ft_fe = ft_fe
        if ft_fe:
            print('Training the front-end')
        if z_bnorm:
            # apply z-norm to the input
            self.z_bnorm = nn.BatchNorm1d(frontend.emb_dim, affine=False)
        if num_spks is None:
            raise ValueError('Please specify a number of spks.')
        self.model = nn.Sequential(
            nn.Conv1d(frontend.emb_dim, hidden_size, 1),
            nn.LeakyReLU(),
            nn.BatchNorm1d(hidden_size),
            nn.Conv1d(hidden_size, num_spks, 1),
            nn.LogSoftmax(dim=1)
        )

    def forward(self, x):
        h = self.frontend(x)
        if not self.ft_fe:
            h = h.detach()
        if hasattr(self, 'z_bnorm'):
            h = self.z_bnorm(h)
        return self.model(h)

class RNNClassifier(Model):

    def __init__(self, frontend,
                 num_spks=None,
                 ft_fe=False,
                 hidden_size=1300,
                 z_bnorm=False,
                 name='RNN'):
        # 1300 default size raises 5.25M params
        super().__init__(name=name)
        self.frontend = frontend
        self.ft_fe = ft_fe
        if z_bnorm:
            # apply z-norm to the input
            self.z_bnorm = nn.BatchNorm1d(frontend.emb_dim, affine=False)
        if num_spks is None:
            raise ValueError('Please specify a number of spks.')
        self.rnn = nn.GRU(frontend.emb_dim, hidden_size // 2,
                          bidirectional=True,
                          batch_first=True)
        self.model = nn.Sequential(
            nn.Conv1d(hidden_size, num_spks, 1),
            nn.LogSoftmax(dim=1)
        )

    def forward(self, x):
        h = self.frontend(x)
        if not self.ft_fe:
            h = h.detach()
        if hasattr(self, 'z_bnorm'):
            h = self.z_bnorm(h)
        ht, state = self.rnn(h.transpose(1, 2))
        y = self.model(ht.transpose(1, 2))
        return y

def select_model(opts, fe, num_spks):
    if opts.model == 'cls':
        model = LinearClassifier(fe, num_spks=num_spks, ft_fe=opts.ft_fe,
                                 z_bnorm=opts.z_bnorm)
    elif opts.model == 'mlp':
        model = MLPClassifier(fe, num_spks=num_spks,
                              hidden_size=opts.hidden_size,
                              ft_fe=opts.ft_fe,
                              z_bnorm=opts.z_bnorm)
    elif opts.model == 'rnn':
        model = RNNClassifier(fe, num_spks=num_spks,
                              hidden_size=opts.hidden_size,
                              ft_fe=opts.ft_fe,
                              z_bnorm=opts.z_bnorm)
    else:
        raise TypeError('Unrecognized model {}'.format(opts.model))
    return model


def main(opts):
    CUDA = torch.cuda.is_available() and not opts.no_cuda
    device = 'cuda' if CUDA else 'cpu'
    torch.manual_seed(opts.seed)
    random.seed(opts.seed)
    np.random.seed(opts.seed)
    if device == 'cuda':
        torch.cuda.manual_seed_all(opts.seed)
    spk2idx = load_spk2idx(opts.spk2idx)
    NSPK=len(set(spk2idx.values()))
    if opts.train:
        #if opts.fe_ckpt is None:
        #    raise ValueError('Please specify a valid ckpt to FE weights')
        with open(os.path.join(opts.save_path, 'train.opts'), 'w') as cfg_f:
            cfg_f.write(json.dumps(vars(opts), indent=2))
        # Open up guia and split valid
        with open(opts.train_guia) as tr_guia_f: 
            tr_files = [l.rstrip() for l in tr_guia_f]
        
        if opts.test_guia is not None:
            with open(opts.test_guia) as te_guia_f: 
                te_files = [l.rstrip() for l in te_guia_f]

        tr_files_, va_files = build_valid_list(tr_files, spk2idx,
                                               va_split=opts.va_split)
        # Build Datasets
        dset = LibriSpkIDDataset(opts.data_root,
                                 tr_files_, spk2idx)
        va_dset = LibriSpkIDDataset(opts.data_root,
                                    va_files, spk2idx)
        cc = WavCollater(max_len=opts.max_len)
        #cc_vate = WavCollater(max_len=None)
        cc_vate = cc
        dloader = DataLoader(dset, batch_size=opts.batch_size, collate_fn=cc,
                             shuffle=True)
        va_dloader = DataLoader(va_dset, batch_size=opts.batch_size,
                                collate_fn=cc_vate,
                                shuffle=False)
        if opts.test_guia is not None:
            te_dset = LibriSpkIDDataset(opts.data_root,
                                        te_files, spk2idx)
            te_dloader = DataLoader(te_dset, batch_size=opts.batch_size,
                                    collate_fn=cc_vate,
                                    shuffle=False)
        # Build Model
        fe = WaveFe(rnn_pool=opts.rnn_pool, emb_dim=opts.emb_dim,
                    inorm_code=opts.inorm_code)
        if opts.fe_ckpt is not None:
            fe.load_pretrained(opts.fe_ckpt, load_last=True, verbose=True)
        else:
            print('*' * 50)
            print('** WARNING: TRAINING WITHOUT PRETRAIED WEIGHTS FOR THE '
                  'FRONT-END **')
            print('*' * 50)
            # Enforce training the frontend
            opts.ft_fe = True
        model = select_model(opts, fe, NSPK)
        model.to(device)
        print(model)
        # Build optimizer and scheduler
        opt = select_optimizer(opts, model)
        sched = lr_scheduler.ReduceLROnPlateau(opt,
            mode=opts.sched_mode,
            factor=opts.lrdec,
            patience=opts.patience,
            verbose=True)
        # Make writer
        writer = SummaryWriter(opts.save_path)
        best_val_acc = 0
        # flag for saver
        best_val = False
        for epoch in range(1, opts.epoch + 1):
            train_epoch(dloader, model, opt, epoch, opts.log_freq, writer=writer,
                        device=device)
            eloss, eacc = eval_epoch(va_dloader, model, epoch, opts.log_freq,
                                     writer=writer, device=device, key='valid')
            sched.step(eacc)
            if eacc > best_val_acc:
                print('New best val acc: {:.3f} => {:.3f}. Patience: {}'
                      ''.format(best_val_acc, eacc, opts.patience))
                best_val_acc = eacc
                best_val = True

            if best_val:
                model.save(opts.save_path, epoch, best_val=best_val)
            best_val = False
            if opts.test_guia is not None:
                # Eval test on the fly with training/valid
                teloss, teacc = eval_epoch(te_dloader, model, epoch, opts.log_freq,
                                           writer=writer, device=device, key='test')
    if opts.test:
        print('Entering test mode')
        fe = WaveFe(rnn_pool=opts.rnn_pool, emb_dim=opts.emb_dim)
        model = select_model(opts, fe, NSPK)
        model.load_pretrained(opts.test_ckpt, load_last=True, verbose=True)
        model.to(device)
        model.eval()
        with open(opts.test_guia) as te_guia_f: 
            te_files = [l.rstrip() for l in te_guia_f]
            te_dset = LibriSpkIDDataset(opts.data_root,
                                        te_files, spk2idx)
            cc = WavCollater(max_len=None)
            te_dloader = DataLoader(te_dset, batch_size=1,
                                    #collate_fn=cc,
                                    shuffle=False)
            def filter_by_slens(T, slens, sfactor=160):
                dims = len(T.size())
                # extract each sequence by its length
                seqs =[]
                for bi in range(T.size(0)):
                    slen = int(np.ceil(slens[bi] / sfactor ))
                    if dims == 3:
                        seqs.append(T[bi, :, :slen])
                    else: 
                        seqs.append(T[bi, :slen])
                return seqs
            with torch.no_grad():
                teloss = []
                teacc = []
                timings = []
                beg_t = timeit.default_timer()
                if opts.test_log_file is not None:
                    test_log_f = open(opts.test_log_file, 'w')
                    test_log_f.write('Filename\tAccuracy [%]\tError [%]\n')
                else:
                    test_log_f = None
                for bidx, batch in enumerate(te_dloader, start=1):
                    #X, Y, slen = batch
                    X, Y = batch
                    X = X.unsqueeze(1)
                    X = X.to(device)
                    Y = Y.to(device)
                    Y_ = model(X)
                    Y = Y.view(-1, 1).repeat(1, Y_.size(2))
                    #Y__seqs = filter_by_slens(Y_, slen)
                    #Y_seqs = filter_by_slens(Y, slen)
                    #assert len(Y__seqs) == len(Y_seqs)
                    #for sidx in range(len(Y__seqs)):
                    #    y_ = Y__seqs[sidx].unsqueeze(0)
                    #    y = Y_seqs[sidx].unsqueeze(0)
                    #    loss = F.nll_loss(y_, y)
                    #    teacc.append(accuracy(y_, y))
                    #    teloss.append(loss)
                    loss = F.nll_loss(Y_, Y)
                    acc = accuracy(Y_, Y)
                    if test_log_f:
                        test_log_f.write('{}\t{:.2f}\t{:.2f}\n' \
                                         ''.format(te_files[bidx - 1],
                                                   acc * 100,
                                                   100 - (acc * 100)))
                    teacc.append(accuracy(Y_, Y))
                    teloss.append(loss)
                    end_t = timeit.default_timer()
                    timings.append(end_t - beg_t)
                    beg_t = timeit.default_timer()
                    if bidx % 100 == 0 or bidx == 1:
                        mteloss = np.mean(teloss)
                        mteacc = np.mean(teacc)
                        mtimings = np.mean(timings)
                    print('Processed test file {}/{} mfiletime: {:.2f} s, '
                          'macc: {:.4f}, mloss: {:.2f}'
                          ''.format(bidx, len(te_dloader), mtimings,
                                    mteacc, mteloss),
                          end='\r')
                print() 
                if test_log_f:
                    test_log_f.write('-' * 30 + '\n')
                    test_log_f.write('Test accuracy: ' \
                                     '{:.2f}\n'.format(np.mean(teacc) * 100))
                    test_log_f.write('Test error: ' \
                                     '{:.2f}\n'.format(100 - (np.mean(teacc) *100)))
                    test_log_f.write('Test loss: ' \
                                     '{:.2f}\n'.format(np.mean(teloss)))
                    test_log_f.close()
                print('Test accuracy: {:.4f}'.format(np.mean(teacc)))
                print('Test loss: {:.2f}'.format(np.mean(teloss)))



def train_epoch(dloader_, model, opt, epoch, log_freq=1, writer=None,
                device='cpu'):
    model.train()
    if not model.ft_fe:
        model.frontend.eval()
    global_idx = epoch * len(dloader_)
    timings = []
    beg_t = timeit.default_timer()
    for bidx, batch in enumerate(dloader_, start=1):
        opt.zero_grad()
        X, Y, slens = batch
        #X = X.transpose(1, 2)
        X = X.unsqueeze(1)
        X = X.to(device)
        Y = Y.to(device)
        Y_ = model(X)
        Y = Y.view(-1, 1).repeat(1, Y_.size(2))
        loss = F.nll_loss(Y_.squeeze(-1), Y)
        loss.backward()
        opt.step()
        end_t = timeit.default_timer()
        timings.append(end_t - beg_t)
        beg_t = timeit.default_timer()
        if bidx % log_freq == 0 or bidx >= len(dloader_):
            acc = accuracy(Y_, Y)
            log_str = 'Batch {:5d}/{:5d} (Epoch {:3d}, Gidx {:5d})' \
                      ' '.format(bidx, len(dloader_),
                                 epoch, global_idx)
            log_str += 'loss: {:.3f} '.format(loss.item())
            log_str += 'bacc: {:.2f} '.format(acc)
            log_str += 'mbtime: {:.3f} s'.format(np.mean(timings))
            print(log_str)
            if writer is not None:
                writer.add_scalar('train/loss', loss.item(),
                                  global_idx)
                writer.add_scalar('train/bacc', acc, global_idx)
        global_idx += 1

def eval_epoch(dloader_, model, epoch, log_freq=1, writer=None, device='cpu',
               key='eval'):
    model.eval()
    with torch.no_grad():
        eval_losses = []
        eval_accs = []
        timings = []
        beg_t = timeit.default_timer()
        for bidx, batch in enumerate(dloader_, start=1):
            X, Y, slens = batch
            #X = X.transpose(1, 2)
            X = X.unsqueeze(1)
            X = X.to(device)
            Y = Y.to(device)
            Y_ = model(X)
            Y = Y.view(-1, 1).repeat(1, Y_.size(2))
            loss = F.nll_loss(Y_, Y)
            eval_losses.append(loss.item())
            acc = accuracy(Y_, Y)
            eval_accs.append(acc)
            end_t = timeit.default_timer()
            timings.append(end_t - beg_t)
            beg_t = timeit.default_timer()
            if bidx % log_freq == 0 or bidx >= len(dloader_):
                
                log_str = 'EVAL::{} Batch {:4d}/{:4d} (Epoch {:3d})' \
                          ' '.format(key, bidx, len(dloader_),
                                     epoch)
                log_str += 'loss: {:.3f} '.format(loss.item())
                log_str += 'bacc: {:.2f} '.format(acc)
                log_str += 'mbtime: {:.3f} s'.format(np.mean(timings))
                print(log_str)
        mloss = np.mean(eval_losses)
        macc = np.mean(eval_accs)
        if writer is not None:
            writer.add_scalar('{}/loss'.format(key), mloss,
                              epoch)
            writer.add_scalar('{}/acc'.format(key), macc, epoch)
        print('EVAL epoch {:3d} mean loss: {:.3f}, mean acc: {:.2f} '
             ''.format(epoch, mloss, macc))
        return mloss, macc


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--save_path', type=str, default='ckpt_mfcc')
    parser.add_argument('--data_root', type=str, default=None)
    parser.add_argument('--batch_size', type=int, default=20)
    parser.add_argument('--train_guia', type=str, default=None)
    parser.add_argument('--test_guia', type=str, default=None)
    parser.add_argument('--spk2idx', type=str, default=None)
    parser.add_argument('--log_freq', type=int, default=50)
    parser.add_argument('--epoch', type=int, default=1000)
    parser.add_argument('--patience', type=int, default=10)
    parser.add_argument('--seed', type=int, default=1234)
    parser.add_argument('--no-cuda', action='store_true', default=False)
    parser.add_argument('--no-rnn', action='store_true', default=False)
    parser.add_argument('--ft_fe', action='store_true', default=False)
    parser.add_argument('--z_bnorm', action='store_true', default=False,
                        help='Use z-norm in z, before any model (Default: '
                             'False).')
    parser.add_argument('--va_split', type=float, default=0.2)
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--max_len', type=int, default=16000)
    parser.add_argument('--hidden_size', type=int, default=256)
    parser.add_argument('--emb_dim', type=int, default=256)
    parser.add_argument('--stats', type=str, default=None)
    parser.add_argument('--opt', type=str, default='adam')
    parser.add_argument('--lrdec', type=float, default=0.1,
                        help='Decay factor of learning rate after '
                             'patience epochs of valid accuracy not '
                             'improving (Def: 0.1).')
    parser.add_argument('--test_ckpt', type=str, default=None)
    parser.add_argument('--fe_ckpt', type=str, default=None)
    parser.add_argument('--sched_mode', type=str, default='max',
                        help='LR Scheduling mode; (1) max, (2) min '
                             '(Def: max).')
    parser.add_argument('--model', type=str, default='cls',
                        help='(1) cls, (2) mlp (Def: cls).')
    parser.add_argument('--train', action='store_true', default=False)
    parser.add_argument('--test', action='store_true', default=False)
    parser.add_argument('--test_log_file', type=str, default=None,
                        help='Possible test log file (Def: None).')
    parser.add_argument('--inorm_code', action='store_true', default=False)
    
    opts = parser.parse_args()
    

    opts.rnn_pool = not opts.no_rnn

    if not os.path.exists(opts.save_path):
        os.makedirs(opts.save_path)

    main(opts)