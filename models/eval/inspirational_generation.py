# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import os
import json
from nevergrad.optimization import optimizerlib
from copy import deepcopy

from PIL import Image
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from nevergrad.optimization.optimizerlib import ParametrizedOnePlusOne
from torch.optim.lbfgs import LBFGS
import platform
from ..gan_visualizer import GANVisualizer
from ..utils.utils import loadmodule, getLastCheckPoint, getVal, \
    getNameAndPackage
from ..utils.image_transform import standardTransform
from ..metrics.nn_score import buildFeatureExtractor
from ..networks.constant_net import FeatureTransform
import sys
sys.path.append('/private/home/broz/workspaces/tests_malagan/malagan/codes/')
from koniq.script_prediction import Koncept512Predictor
dir_root = "/Users/broz/workspaces/tests_malagan/malagan/" if platform == "darwin" else "/private/home/broz/workspaces/tests_malagan/malagan/"


def pil_loader(path):

    # open path as file to avoid ResourceWarning
    # (https://github.com/python-pillow/Pillow/issues/835)
    with open(path, 'rb') as f:
        img = Image.open(f)
        return img.convert('RGB')


def getFeatireSize(x):

    s = x.size()
    out = 1
    for p in s[1:]:
        out *= p

    return out


class IDModule(nn.Module):

    def __init__(self):

        super(IDModule, self).__init__()
        # self.dummy = nn.Conv2d(1,1,1,1)

    def forward(self, x):
        return x.view(-1, getFeatireSize(x))


def updateParser(parser):

    parser.add_argument('-N', type=int, dest="nRuns",
                        help="Number of gradient descent to run at the same \
                        time. Being too greedy may result in memory error.",
                        default=1)
    parser.add_argument('-l', type=float, dest="learningRate",
                        help="Learning rate",
                        default=1)
    parser.add_argument('-S', '--suffix', type=str, dest='suffix',
                        help="Output's suffix", default="inspiration")
    parser.add_argument('--nSteps', type=int, dest='nSteps',
                        help="Number of steps", default=6000)
    parser.add_argument('--weights', type=float, dest='weights',
                        nargs='*', help="Weight of each classifier. Default \
                        value is one. If specified, the number of weights must\
                        match the number of feature exatrcators.")
    parser.add_argument('--gradient_descent', help='gradient descent',
                        action='store_true')
    parser.add_argument('--random_search', help='Random search',
                        action='store_true')
    parser.add_argument('--size', type=int, help="Size of the input of the \
                        feature map", default=128)
    parser.add_argument('--nevergrad', type=str,
                        choices=['CMA', 'RandomSearch', 'DE', 'PSO', 'TwoPointsDE',
                                 'PortfolioDiscreteOnePlusOne',
                                 'DiscreteOnePlusOne', 'OnePlusOne', 'LBFGS'])
    parser.add_argument('--save_descent', help='Save descent',
                        action='store_true')

    return parser


def run_frugan(model,
               scorer,
                           visualizer=None,
                           nSteps=6000,
                           randomSearch=False,
                           nevergrad=None,
                           outPathSave=None):
    r"""
    Performs a similarity search with gradient descent.

    Args:

        model (BaseGAN): trained GAN model to use
        visualizer (visualizer): if not None, visualizer to use to plot
                                 intermediate results
        lambdaD (float): weight of the realism loss
        nSteps (int): number of steps to perform
        randomSearch (bool): if true, replace tha gradient descent by a random
                             search
        nevergrad (string): must be in None or in ['CMA', 'DE', 'PSO',
                            'TwoPointsDE', 'PortfolioDiscreteOnePlusOne',
                            'DiscreteOnePlusOne', 'OnePlusOne']
        outPathSave (string): if not None, path to save the intermediate
                              iterations of the gradient descent
    Returns

        output, optimalVector, optimalLoss

        output (tensor): output images
        optimalVector (tensor): latent vectors corresponding to the output
                                images
    """

    if nevergrad not in [None, 'CMA', 'DE', 'PSO',
                         'TwoPointsDE', 'PortfolioDiscreteOnePlusOne',
                         'DiscreteOnePlusOne', 'OnePlusOne', 'RandomSearch', 'LBFGS']:
        raise ValueError("Invalid nevergard mode " + str(nevergrad))

    randomSearch = randomSearch or (nevergrad is not None and nevergrad != 'LBFGS')
    print("Running for %d setps" % nSteps)

    nImages = 1
    # Detect categories
    noise_dim = model.config.noiseVectorDim + model.config.categoryVectorDim
    # varNoise = torch.randn((nImages,
    #                         noise_dim),
    #                        requires_grad=True, device=model.device)
    start_noise = np.random.normal(size=noise_dim)

    # noiseOut = model.test(varNoise, getAvG=True, toCPU=False)

    optimalVector = None
    optimalLoss = None

    print(f"Generating {nImages} images")
    assert nevergrad is not None
    optimizers = []
    PortfolioDiscreteOnePlusOne = ParametrizedOnePlusOne(mutation="portfolio").set_name("PortfolioDiscreteOnePlusOne", register=True)
    for i in range(nImages):
        optimizers += [optimizerlib.registry[nevergrad](
            parametrization=noise_dim,
            budget=nSteps)]
        optimizers[i].suggest(start_noise)

    # String's format for loss output
    formatCommand = ' '.join(['{:>4}' for x in range(nImages)])
    important_iter = { 0,10, 20, 40, 80, 160, 320, 640}
    for iter in range(nSteps):

        if randomSearch:
            inps = []
            for i in range(nImages):
                inps += [optimizers[i].ask()]
                npinps = np.array([inp.value for inp in inps])
            varNoise = torch.tensor(
                npinps, dtype=torch.float32, device=model.device)
            varNoise.requires_grad = False
            varNoise.to(model.device)

        noiseOut = model.netG(varNoise)

        s = scorer.predict(np.moveaxis(noiseOut.cpu().detach().numpy(), 1, -1))
        # print(s)
        loss = - s[0]

        if nevergrad:
            for i in range(nImages):
                optimizers[i].tell(inps[i], float(loss))

        if optimalLoss is None:
            optimalVector = deepcopy(varNoise)
            optimalLoss = loss
        else:
            optimalVector = varNoise if loss < optimalLoss else optimalVector
            optimalLoss = loss if loss < optimalLoss else optimalLoss

        if iter % 100 == 0 or iter in important_iter:
            if visualizer is not None:
                visualizer.publishTensors(noiseOut.cpu(), (128, 128))

                if outPathSave is not None:
                    index_str = str(int(iter))

                    outPath = os.path.join(outPathSave, index_str + ".jpg")
                    visualizer.saveTensor(
                        noiseOut.cpu().detach(),
                        (noiseOut.size(2), noiseOut.size(3)),
                        outPath)

                    imgOpt = model.netG(optimalVector)
                    outPathOpt = os.path.join(outPathSave, index_str + "_opt.jpg")
                    print(f'saving in {outPathOpt}')
                    visualizer.saveTensor(
                        imgOpt.cpu().detach(),
                        (imgOpt.size(2), imgOpt.size(3)),
                        outPathOpt)

            print(str(iter) + " : " + formatCommand.format(
                *["{:10.6f}".format(loss, optimalLoss)
                  for i in range(nImages)]))

    output = model.test(optimalVector, getAvG=True, toCPU=True).detach()

    if visualizer is not None:
        visualizer.publishTensors(
            output.cpu(), (output.size(2), output.size(3)))

    print("optimal losses : " + formatCommand.format(
        *["{:10.6f}".format(optimalLoss[i].item())
          for i in range(nImages)]))
    return output, optimalVector, optimalLoss

# def gradientDescentOnInput(model,
#                            input,
#                            visualizer=None,
#                            lambdaD=0.03,
#                            nSteps=6000,
#                            randomSearch=False,
#                            nevergrad=None,
#                            lr=1,
#                            outPathSave=None):
#     r"""
#     Performs a similarity search with gradient descent.
#
#     Args:
#
#         model (BaseGAN): trained GAN model to use
#         input (tensor): inspiration images for the gradient descent. It should
#                         be a [NxCxWxH] tensor with N the number of image, C the
#                         number of color channels (typically 3), W the image
#                         width and H the image height
#         visualizer (visualizer): if not None, visualizer to use to plot
#                                  intermediate results
#         lambdaD (float): weight of the realism loss
#         nSteps (int): number of steps to perform
#         randomSearch (bool): if true, replace tha gradient descent by a random
#                              search
#         nevergrad (string): must be in None or in ['CMA', 'DE', 'PSO',
#                             'TwoPointsDE', 'PortfolioDiscreteOnePlusOne',
#                             'DiscreteOnePlusOne', 'OnePlusOne']
#         outPathSave (string): if not None, path to save the intermediate
#                               iterations of the gradient descent
#     Returns
#
#         output, optimalVector, optimalLoss
#
#         output (tensor): output images
#         optimalVector (tensor): latent vectors corresponding to the output
#                                 images
#     """
#
#     if nevergrad not in [None, 'CMA', 'DE', 'PSO',
#                          'TwoPointsDE', 'PortfolioDiscreteOnePlusOne',
#                          'DiscreteOnePlusOne', 'OnePlusOne', 'LBFGS']:
#         raise ValueError("Invalid nevergard mode " + str(nevergrad))
#     use_lbfgs = False
#     if nevergrad == 'LBFGS':
#         nevergrad = None
#         use_lbfgs=True
#     randomSearch = randomSearch or (nevergrad is not None and nevergrad != 'LBFGS')
#     print("Running for %d setps" % nSteps)
#
#     if visualizer is not None:
#         visualizer.publishTensors(input, (128, 128))
#
#     # Detect categories
#     varNoise = torch.randn((input.size(0),
#                             model.config.noiseVectorDim +
#                             model.config.categoryVectorDim),
#                            requires_grad=True, device=model.device)
#
#     if use_lbfgs:
#         optimNoise = LBFGS([varNoise], lr=1)
#     else:
#         optimNoise = optim.Adam([varNoise], betas=[0., 0.99], lr=lr)
#
#     noiseOut = model.test(varNoise, getAvG=True, toCPU=False)
#     lr = 1
#
#     optimalVector = None
#     optimalLoss = None
#
#     epochStep = int(nSteps / 3)
#     gradientDecay = 0.1
#
#     nImages = input.size(0)
#     print(f"Generating {nImages} images")
#     if nevergrad is not None:
#         optimizers = []
#         for i in range(nImages):
#             optimizers += [optimizerlib.registry[nevergrad](
#                 parametrization=model.config.noiseVectorDim +
#                 model.config.categoryVectorDim,
#                 budget=nSteps)]
#
#     def resetVar(newVal):
#         newVal.requires_grad = True
#         print("Updating the optimizer with learning rate : %f" % lr)
#         varNoise = newVal
#         optimNoise = optim.Adam([varNoise], betas=[0., 0.99], lr=lr)
#
#     # String's format for loss output
#     formatCommand = ' '.join(['{:>4}' for x in range(nImages)])
#     backtobfgs = False
#
#     for iter in range(nSteps):
#
#         optimNoise.zero_grad()
#         model.netG.zero_grad()
#
#         if randomSearch:
#             varNoise = torch.randn((nImages,
#                                     model.config.noiseVectorDim +
#                                     model.config.categoryVectorDim),
#                                    device=model.device)
#             if nevergrad:
#                 inps = []
#                 for i in range(nImages):
#                     inps += [optimizers[i].ask()]
#                     npinps = np.array([inp.value for inp in inps])
#                 varNoise = torch.tensor(
#                     npinps, dtype=torch.float32, device=model.device)
#                 varNoise.requires_grad = True
#                 varNoise.to(model.device)
#
#         noiseOut = model.netG(varNoise)
#         sumLoss = torch.zeros(nImages, device=model.device)
#
#         loss = (((varNoise**2).mean(dim=1) - 1)**2)
#         sumLoss += loss.view(nImages)
#         loss.sum(dim=0).backward(retain_graph=True)
#
#         if nevergrad:
#             for i in range(nImages):
#                 optimizers[i].tell(inps[i], float(sumLoss[i]))
#         elif not randomSearch:
#             optimNoise.step(closure=lambda:loss.sum(dim=0))
#
#         if optimalLoss is None:
#             optimalVector = deepcopy(varNoise)
#             optimalLoss = sumLoss
#
#         else:
#             optimalVector = torch.where(sumLoss.view(-1, 1) < optimalLoss.view(-1, 1),
#                                         varNoise, optimalVector).detach()
#             optimalLoss = torch.where(sumLoss < optimalLoss,
#                                       sumLoss, optimalLoss).detach()
#
#         if iter % 100 == 0:
#             if visualizer is not None:
#                 visualizer.publishTensors(noiseOut.cpu(), (128, 128))
#
#                 if outPathSave is not None:
#                     index_str = str(int(iter/100))
#                     outPath = os.path.join(outPathSave, index_str + ".jpg")
#                     visualizer.saveTensor(
#                         noiseOut.cpu(),
#                         (noiseOut.size(2), noiseOut.size(3)),
#                         outPath)
#
#             print(str(iter) + " : " + formatCommand.format(
#                 *["{:10.6f}".format(sumLoss[i].item())
#                   for i in range(nImages)]))
#         if use_lbfgs:
#             for i in range(nImages):
#                 if sumLoss[i] != sumLoss[i]:
#                     #   varNoise[i].data = optimalVector[i].data
#                     varNoise.data = optimalVector.data
#                     optimNoise = optim.Adam([varNoise], lr=lr)
#                     backtobfgs = True
#         if backtobfgs:
#             optimNoise = torch.optim.LBFGS([varNoise], lr=lr)
#             backtobfgs = False
#
#         if iter % epochStep == (epochStep - 1):
#             lr *= gradientDecay
#             resetVar(optimalVector)
#
#     output = model.test(optimalVector, getAvG=True, toCPU=True).detach()
#
#     if visualizer is not None:
#         visualizer.publishTensors(
#             output.cpu(), (output.size(2), output.size(3)))
#
#     print("optimal losses : " + formatCommand.format(
#         *["{:10.6f}".format(optimalLoss[i].item())
#           for i in range(nImages)]))
#     return output, optimalVector, optimalLoss


def test(parser, visualisation=None):

    parser = updateParser(parser)

    kwargs = vars(parser.parse_args())

    # Parameters
    name = getVal(kwargs, "name", None)
    if name is None:
        raise ValueError("You need to input a name")

    module = getVal(kwargs, "module", None)
    if module is None:
        raise ValueError("You need to input a module")

    imgPath = getVal(kwargs, "inputImage", None)

    scale = getVal(kwargs, "scale", None)
    iter = getVal(kwargs, "iter", None)
    nRuns = getVal(kwargs, "nRuns", 1)

    checkPointDir = os.path.join(kwargs["dir"], name)
    checkpointData = getLastCheckPoint(checkPointDir,
                                       name,
                                       scale=scale,
                                       iter=iter)
    weights = getVal(kwargs, 'weights', None)

    if checkpointData is None:
        raise FileNotFoundError(
            "No checkpoint found for model " + str(name) + " at directory "
            + str(checkPointDir) + ' cwd=' + str(os.getcwd()))

    modelConfig, pathModel, _ = checkpointData

    keysLabels = None
    with open(modelConfig, 'rb') as file:
        keysLabels = json.load(file)["attribKeysOrder"]
    if keysLabels is None:
        keysLabels = {}

    packageStr, modelTypeStr = getNameAndPackage(module)
    modelType = loadmodule(packageStr, modelTypeStr)

    visualizer = GANVisualizer(
        pathModel, modelConfig, modelType, visualisation)

    # Load the image
    targetSize = visualizer.model.getSize()

    basePath = os.path.join('/private/home/broz/workspaces/pytorch_GAN_zoo/outputs', 'frugan', f'iter_{kwargs["nSteps"]}', kwargs["nevergrad"],
                            kwargs['suffix'], f'iter_{kwargs["nSteps"]}')

    mkdir('/'.join(basePath.split('/')[:-2]))
    mkdir('/'.join(basePath.split('/')[:-1]))
    mkdir(basePath)

    # basePath = os.path.join(basePath) #os.path.basename(basePath))

    print("All results will be saved in " + basePath)

    outDictData = {}
    outPathDescent = None

    if kwargs['save_descent']:
        outPathDescent = os.path.join(
            os.path.dirname(basePath), "descent")
        if not os.path.isdir(outPathDescent):
            os.mkdir(outPathDescent)
    scorer = Koncept512Predictor(aux_root=dir_root + "codes/koniq/", compute_grads=True)

    img, outVectors, loss = run_frugan(visualizer.model,
                                                   scorer=scorer,
                                                   visualizer=visualisation,
                                                   nSteps=kwargs['nSteps'],
                                                   randomSearch=kwargs['random_search'],
                                                   nevergrad=kwargs['nevergrad'],
                                                   outPathSave=outPathDescent)

    pathVectors = basePath + "vector.pt"
    torch.save(outVectors, open(pathVectors, 'wb'))

    path = basePath + ".jpg"

    if os.path.isfile(path):
        number = 1
        while True:
            number += 1
            new_path = path.split(".jpg")[0] + str(number) + ".jpg"
            if os.path.isfile(new_path):
                continue
            else:
                path = new_path
                break

    if visualisation:
        visualisation.saveTensor(img, (img.size(2), img.size(3)), path)

    outDictData[os.path.splitext(os.path.basename(path))[0]] = \
        [x.item() for x in loss]

    outVectors = outVectors.view(outVectors.size(0), -1)
    outVectors *= torch.rsqrt((outVectors**2).mean(dim=1, keepdim=True))

    barycenter = outVectors.mean(dim=0)
    barycenter *= torch.rsqrt((barycenter**2).mean())
    meanAngles = (outVectors * barycenter).mean(dim=1)
    meanDist = torch.sqrt(((barycenter-outVectors)**2).mean(dim=1)).mean(dim=0)
    outDictData["Barycenter"] = {"meanDist": meanDist.item(),
                                 "stdAngles": meanAngles.std().item(),
                                 "meanAngles": meanAngles.mean().item()}

    path = basePath + "_data.json"
    outDictData["kwargs"] = kwargs

    with open(path, 'w') as file:
        json.dump(outDictData, file, indent=2)

    pathVectors = basePath + "vectors.pt"
    torch.save(outVectors, open(pathVectors, 'wb'))


def mkdir(basePath):
    if not os.path.isdir(basePath):
        os.mkdir(basePath)
