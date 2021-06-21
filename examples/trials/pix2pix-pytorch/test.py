import sys
sys.path.insert(0, './pix2pixlib')

import os
import logging
import argparse
import json
from collections import namedtuple
from PIL import Image
import numpy as np
import torch
from nni.utils import merge_parameter
from pix2pixlib.data.aligned_dataset import AlignedDataset
from pix2pixlib.data import CustomDatasetDataLoader
from pix2pixlib.models.pix2pix_model import Pix2PixModel
from base_params import get_base_params


_logger = logging.getLogger('example_pix2pix')


def download_dataset(dataset_name):
    # code adapted from https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix
    assert(dataset_name in ['facades', 'night2day', 'edges2handbags', 'edges2shoes', 'maps'])
    if os.path.exists('./data/' + dataset_name):
        _logger.info("Already downloaded dataset " + dataset_name)
    else:
        _logger.info("Downloading dataset " + dataset_name)
        if not os.path.exists('./data/'):
            os.system('mkdir ./data')
        os.system('mkdir ./data/' + dataset_name)
        URL = 'http://efrosgans.eecs.berkeley.edu/pix2pix/datasets/{}.tar.gz'.format(dataset_name)
        TAR_FILE = './data/{}.tar.gz'.format(dataset_name)
        TARGET_DIR = './data/{}/'.format(dataset_name)
        os.system('wget -N {} -O {}'.format(URL, TAR_FILE))
        os.system('mkdir -p {}'.format(TARGET_DIR))
        os.system('tar -zxvf {} -C ./data/'.format(TAR_FILE))
        os.system('rm ' + TAR_FILE)        

        
def parse_args():
    parser = argparse.ArgumentParser(description='PyTorch Pix2pix Example')

    # required arguments
    parser.add_argument('-c', '--checkpoint', type=str, required=True,
                        help='Checkpoint directory')
    parser.add_argument('-p', '--parameter_cfg', type=str, required=True,
                        help='parameter.cfg file generated by nni trial')
    parser.add_argument('-d', '--dataset', type=str, required=True,
                        help='dataset name (facades, night2day, edges2handbags, edges2shoes, maps)')
    parser.add_argument('-o', '--output_dir', type=str, required=True,
                        help='Where to save the test results')
    
    # Settings that may be overrided by parameters from nni
    parser.add_argument('--ngf', type=int, default=64, 
                        help='# of generator filters in the last conv layer')
    parser.add_argument('--ndf', type=int, default=64,
                        help='# of discriminator filters in the first conv layer')
    parser.add_argument('--netD', type=str, default='basic',
                        help='specify discriminator architecture [basic | n_layers | pixel]. The basic model is a 70x70 PatchGAN. n_layers allows you to specify the layers in the discriminator')
    parser.add_argument('--netG', type=str, default='resnet_9blocks',
                        help='specify generator architecture [resnet_9blocks | resnet_6blocks | unet_256 | unet_128]')
    parser.add_argument('--init_type', type=str, default='normal',
                        help='network initialization [normal | xavier | kaiming | orthogonal]')
    parser.add_argument('--beta1', type=float, default=0.5,
                        help='momentum term of adam')
    parser.add_argument('--lr', type=float, default=0.0002,
                        help='initial learning rate for adam')
    parser.add_argument('--lr_policy', type=str, default='linear',
                        help='learning rate policy. [linear | step | plateau | cosine]')
    parser.add_argument('--gan_mode', type=str, default='lsgan',
                        help='the type of GAN objective. [vanilla| lsgan | wgangp]. vanilla GAN loss is the cross-entropy objective used in the original GAN paper.')
    parser.add_argument('--norm', type=str, default='instance',
                        help='instance normalization or batch normalization [instance | batch | none]')
    parser.add_argument('--lambda_L1', type=float, default=100,
                        help='weight of L1 loss in the generator objective')
    
    # Additional training settings 
    parser.add_argument('--batch_size', type=int, default=1,
                        help='input batch size for training (default: 1)')
    parser.add_argument('--n_epochs', type=int, default=100,
                        help='number of epochs with the initial learning rate')
    parser.add_argument('--n_epochs_decay', type=int, default=100,
                        help='number of epochs to linearly decay learning rate to zero')
    
    args, _ = parser.parse_known_args()
    return args



def tensor2im(input_image, imtype=np.uint8):
    """ 
    Code adapted from https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix 
    Converts a Tensor array into a numpy image array.
    Parameters:
        input_image (tensor) --  the input image tensor array
        imtype (type)        --  the desired type of the converted numpy array
    """
    if not isinstance(input_image, np.ndarray):
        if isinstance(input_image, torch.Tensor):  # get the data from a variable
            image_tensor = input_image.data
        else:
            return input_image
        image_numpy = image_tensor[0].cpu().float().numpy()  # convert it into a numpy array
        if image_numpy.shape[0] == 1:  # grayscale to RGB
            image_numpy = np.tile(image_numpy, (3, 1, 1))
        image_numpy = (np.transpose(image_numpy, (1, 2, 0)) + 1) / 2.0 * 255.0  # post-processing: tranpose and scaling
    else:  # if it is a numpy array, do nothing
        image_numpy = input_image
    return image_numpy.astype(imtype)


def main(test_params):
    test_config = namedtuple('Struct', test_params.keys())(*test_params.values())
    assert os.path.exists(test_config.checkpoint), "Checkpoint does not exist"

    download_dataset(test_config.dataset)
    
    test_dataset = AlignedDataset(test_config)
    test_dataset = CustomDatasetDataLoader(test_config, test_dataset)
    _logger.info('Number of testing images = {}'.format(len(test_dataset)))    

    model = Pix2PixModel(test_config)
    model.setup(test_config)

    if test_config.eval:
        model.eval()

    for i, data in enumerate(test_dataset):
        print('Testing on {} image {}'.format(test_config.dataset, i), end='\r')
        model.set_input(data)  
        model.test()

        visuals = model.get_current_visuals()
        cur_input = tensor2im(visuals['real_A'])
        cur_label = tensor2im(visuals['real_B'])
        cur_output = tensor2im(visuals['fake_B'])
        
        image_name = '{}_test_{}.png'.format(test_config.dataset, i)
        Image.fromarray(cur_input).save(os.path.join(test_config.output_dir, 'input', image_name))
        Image.fromarray(cur_label).save(os.path.join(test_config.output_dir, 'label', image_name))
        Image.fromarray(cur_output).save(os.path.join(test_config.output_dir, 'output', image_name))

    _logger.info("Images successfully saved to " + test_config.output_dir)

    
if __name__ == '__main__':
    params_from_cl = vars(parse_args())
    _, test_params = get_base_params(params_from_cl['dataset'], params_from_cl['checkpoint'])  
    test_params.update(params_from_cl)

    with open(test_params['parameter_cfg'], 'r') as f:
        params_from_nni = json.loads(f.readline().strip())['parameters']
    test_params = merge_parameter(test_params, params_from_nni)

    os.system('mkdir -p {}/input'.format(params_from_cl['output_dir']))
    os.system('mkdir -p {}/label'.format(params_from_cl['output_dir']))
    os.system('mkdir -p {}/output'.format(params_from_cl['output_dir']))

    main(test_params)
    
