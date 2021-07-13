import os
import sys
import timeit
import torch
import argparse
import os.path as osp
import numpy as np
import torch.nn as nn
import torch.optim as optim
from torch.utils import data
import matplotlib.pylab as plt
from networks.unet import unet
from torch import einsum
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
from torch.nn import functional as F
from dataset.datasets import XRAYDataSet
from tensorboardX import SummaryWriter
from utils.encoding import DataParallelModel, DataParallelCriterion

torch_ver = torch.__version__[:3]
if torch_ver == '0.3':
    from torch.autograd import Variable

start = timeit.default_timer()

IMG_MEAN = np.array((104.00698793,116.66876762,122.67891434), dtype=np.float32)

BATCH_SIZE = 8
DATA_DIRECTORY = './data'
DATA_LIST_PATH = './data/train.txt'
IGNORE_LABEL = 0
INPUT_SIZE = '512,512'
LEARNING_RATE = 1e-2
MOMENTUM = 0.9
NUM_CLASSES = 1
NUM_STEPS = 10000
POWER = 0.9
RANDOM_SEED = 1234
RESTORE_FROM = './dataset/resnet101-imagenet.pth'
SAVE_NUM_IMAGES = 2
SAVE_PRED_EVERY = 250
SNAPSHOT_DIR = './snapshot/'
WEIGHT_DECAY = 0.0005

def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def get_arguments():
    parser = argparse.ArgumentParser(description="XLSor Network")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help="Number of images sent to the network in one step.")
    parser.add_argument("--data-dir", type=str, default=DATA_DIRECTORY,
                        help="Path to the directory containing the PASCAL VOC dataset.")
    parser.add_argument("--data-list", type=str, default=DATA_LIST_PATH,
                        help="Path to the file listing the images in the dataset.")
    parser.add_argument("--ignore-label", type=int, default=IGNORE_LABEL,
                        help="The index of the label to ignore during the training.")
    parser.add_argument("--input-size", type=str, default=INPUT_SIZE,
                        help="Comma-separated string with height and width of images.")
    parser.add_argument("--is-training", action="store_true",
                        help="Whether to updates the running means and variances during the training.")
    parser.add_argument("--learning-rate", type=float, default=LEARNING_RATE,
                        help="Base learning rate for training with polynomial decay.")
    parser.add_argument("--momentum", type=float, default=MOMENTUM,
                        help="Momentum component of the optimiser.")
    parser.add_argument("--not-restore-last", action="store_true",
                        help="Whether to not restore last (FC) layers.")
    parser.add_argument("--num-classes", type=int, default=NUM_CLASSES,
                        help="Number of classes to predict (including background).")
    parser.add_argument("--start-iters", type=int, default=0,
                        help="Number of classes to predict (including background).")
    parser.add_argument("--num-steps", type=int, default=NUM_STEPS,
                        help="Number of training steps.")
    parser.add_argument("--power", type=float, default=POWER,
                        help="Decay parameter to compute the learning rate.")
    parser.add_argument("--random-mirror", action="store_true",
                        help="Whether to randomly mirror the inputs during the training.")
    parser.add_argument("--random-scale", action="store_true",
                        help="Whether to randomly scale the inputs during the training.")
    parser.add_argument("--random-seed", type=int, default=RANDOM_SEED,
                        help="Random seed to have reproducible results.")
    parser.add_argument("--restore-from", type=str, default=RESTORE_FROM,
                        help="Where restore model parameters from.")
    parser.add_argument("--save-num-images", type=int, default=SAVE_NUM_IMAGES,
                        help="How many images to save.")
    parser.add_argument("--save-pred-every", type=int, default=SAVE_PRED_EVERY,
                        help="Save summaries and checkpoint every often.")
    parser.add_argument("--snapshot-dir", type=str, default=SNAPSHOT_DIR,
                        help="Where to save snapshots of the model.")
    parser.add_argument("--weight-decay", type=float, default=WEIGHT_DECAY,
                        help="Regularisation parameter for L2-loss.")
    parser.add_argument("--gpu", type=str, default='0,1,2,3',
                        help="choose gpu device.")
    parser.add_argument("--recurrence", type=int, default=2,
                        help="choose the number of recurrence.")
    parser.add_argument("--ft", type=bool, default=False,
                        help="fine-tune the model with large input size.")

    return parser.parse_args()

args = get_arguments()

def lr_poly(base_lr, iter, max_iter, power):
    return base_lr*((1-float(iter)/max_iter)**(power))
            
def adjust_learning_rate(optimizer, i_iter):
    lr = lr_poly(args.learning_rate, i_iter, args.num_steps, args.power)
    optimizer.param_groups[0]['lr'] = lr
    return lr

def set_bn_eval(m):
    classname = m.__class__.__name__
    if classname.find('BatchNorm') != -1:
        m.eval()

def set_bn_momentum(m):
    classname = m.__class__.__name__
    if classname.find('BatchNorm') != -1 or classname.find('InPlaceABN') != -1:
        m.momentum = 0.0003
        
def main():
    writer = SummaryWriter(args.snapshot_dir)
    if not args.gpu == 'None':
        os.environ["CUDA_VISIBLE_DEVICES"]= '0'#args.gpu
    h, w = map(int, args.input_size.split(','))
    input_size = (h, w)

    cudnn.enabled = True

    model = unet()
    model.train()
    model.float()
    model.cuda()    
    
    criterion = nn.MSELoss()
    #criterion = DataParallelCriterion(criterion)
    criterion.cuda()
    
    cudnn.benchmark = True

    if not os.path.exists(args.snapshot_dir):
        os.makedirs(args.snapshot_dir)

    trainloader = data.DataLoader(XRAYDataSet(args.data_dir, args.data_list, max_iters=args.num_steps*args.batch_size, crop_size=input_size,
                    scale=args.random_scale, mirror=args.random_mirror, mean=IMG_MEAN), 
                    batch_size=args.batch_size, shuffle=True, num_workers=16, pin_memory=True)

    optimizer = optim.SGD([{'params': filter(lambda p: p.requires_grad, xlsor.parameters()), 'lr': args.learning_rate}],
                          lr=args.learning_rate, momentum=args.momentum, weight_decay=args.weight_decay)

    optimizer = torch.optim.Adam(xlsor.parameters())
    optimizer.zero_grad()

    loss_values = []
    best_model = 5

    for i_iter, batch in enumerate(trainloader):
        i_iter += args.start_iters
        
        images, labels, name = batch
        images = images.cuda()
        labels = labels.float().cuda()

        if torch_ver == "0.3":
            images = Variable(images)
            labels = Variable(labels)

        optimizer.zero_grad()
        lr = adjust_learning_rate(optimizer, i_iter)

        preds = model(images)

        loss = criterion(preds, labels)
        loss.backward()
        optimizer.step()

        print('iter = {} of {} completed, loss = {}'.format(i_iter, args.num_steps, loss.data.cpu().numpy()))
        print('------------------------------------------------')

        if best_model >= loss.data.cpu().numpy():
            best_model = loss.data.cpu().numpy()
            torch.save(xlsor.state_dict(), osp.join(args.snapshot_dir, 'unet__' + str(i_iter) + '_' + str(
                loss.data.cpu().numpy()) + '.pth'))

        if i_iter > args.num_steps - 1:
            print('finished ...')
            break

    end = timeit.default_timer()
    print(end - start, 'seconds')

if __name__ == '__main__':
    main()
