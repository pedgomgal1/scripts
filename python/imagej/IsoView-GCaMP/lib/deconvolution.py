from net.imglib2 import FinalInterval
from net.imglib2.img.array import ArrayImgFactory
from net.imglib2.type.numeric.real import FloatType
from net.imglib2.view import Views
from net.preibisch.mvrecon.process.deconvolution import MultiViewDeconvolutionSeq, DeconView, DeconViews
from net.preibisch.mvrecon.process.deconvolution.iteration.sequential import ComputeBlockSeqThreadCPUFactory, ComputeBlockSeqThreadCUDAFactory
from net.preibisch.mvrecon.process.deconvolution.init import PsiInitBlurredFusedFactory
from net.preibisch.mvrecon.process.deconvolution.DeconViewPSF import PSFTYPE
from net.preibisch.mvrecon.process.fusion.transformed import TransformView
from net.preibisch.mvrecon.process.cuda.CUDAFourierConvolution
from bdv.util import ConstantRandomAccessible
from itertools import repeat, izip
# local lib functions:
from util import newFixedThreadPool, syncPrint


def setupEngine(use_cuda=True):
  """
  Attempt to load the CUDA libraries. Otherwise use CPU threads.
  Return a function that creates the ComputeBlockSeqThread(CPU|CUDA)Factory.

  For the CUDA version to work, do, in Ubuntu 16.04:
   1. Download the .deb file for CUDA 10.0 from:
     https://developer.nvidia.com/compute/cuda/10.0/Prod/local_installers/cuda-repo-ubuntu1604-10-0-local-10.0.130-410.48_1.0-1_amd64
   2. Install the CUDA deb package and more:
      $ sudo dpkg -i cuda-repo-ubuntu1604-10-0-*deb
      $ sudo apt-key add /var/cuda-repo-10-0-local-10.0.130-410.48/7fa2af80.pub
      $ sudo apt-get update
      $ sudo apt-get install cuda
   3. Download the FourierConvolutionCUDALib from:
     https://github.com/StephanPreibisch/FourierConvolutionCUDALib
   4. Install it:
      $ cd FourierConvolutionCUDALib/
      $ mkdir build
      $ cd build
      $ cmake ..
      $ make
      $ sudo make install
  """
  cuda = None
  devices = []
  idToCudaDevice = {}
  if use_cuda:
    cuda = NativeLibraryTools.loadNativeLibrary(["fftCUDA", "FourierConvolutionCuda"], CUDAFourierConvolution)
    if not cuda:
      syncPrint("Could not load CUDA JNA library for FFT convolution.")
    else:
      syncPrint("Will use CUDA for FFT convolution.")
      devices = CUDATools.queryCUDADetails(cuda, True)
      idToCudaDevice = {index: device for index, device in enumerate(devices)}
  # Return function
  def createFactory(exe, lambda_val, blockSize, factory=None):
    factory = factory if factory else ArrayImgFactory(FloatType())
    if use_cuda and cuda:
      return ComputeBlockSeqThreadCUDAFactory(exe, lambda_val, blockSize, factory, cuda, idToCudaDevice)
    else:
      return ComputeBlockSeqThreadCPUFactory(exe, lambda_val, blockSize, factory)

createFactory = setupEngine()


def multiviewDeconvolution(images, blockSize, PSF_kernel, n_iterations, lambda_val=0.0006, weights=None,
                           filterBlocksForContent=False, PSF_type=PSFTYPE.INDEPENDENT, exe=None, printFn=syncPrint):
  """
  Apply Bayesian-based multi-view deconvolution to the list of images,
  returning the deconvolved image. Uses Stephan Preibisch's library,
  currently available with the BigStitcher Fiji update site.

  images: a list of images, registered and with the same dimensions.
  blockSize: how to chop up the volume for parallel processing.
  PSF_kernel: the image containing the point spread function. Requirement: its dimensions must be an odd number.
  n_iterations: the number of iterations for the deconvolution. A number between 10 and 50 is desirable. The more iterations, the higher the computational cost.
  lambda_val: default is 0.0006 as recommended by Preibisch.
  weights: a list of FloatType images with the weight for every pixel. If None, then all pixels get a value of 1.
  filterBlocksForContent: whether to check before processing a block if the block has any data in it. Default is False.
  PSF_type: defaults to PSFTYPE.INDEPENDENT.
  exe: a thread pool for concurrent execution. If None, a new one is created, using as many threads as CPUs are available.
  printFn: the function to use for printing error messages. Defaults to syncPrint (thread-safe access to the built-in `print` function).

  Returns an imglib2 ArrayImg, or None if something went wrong.
  """

  mvd_exe = exe
  if not exe:
    mvd_exe = newFixedThreadPool() # as many threads as CPUs

  try:
    mvd_weights = weights
    if not weights:
      mvd_weights = repeat(Views.interval(ConstantRandomAccessible(FloatType(1), images[0].numDimensions()), FinalInterval(images[0])))

    for d in xrange(PSF_kernel.numDimensions()):
      if 0 == PSF_kernel.dimension(d) % 2:
        printFn("PSF kernel dimension %i is not odd." % d)
        return None

    factory = ArrayImgFactory(FloatType())
    cptf = createFactory(exe, lambda_val, blockSize, factory)
    filterBlocksForContent = False # Run once with True, none were removed
    decon_views = DeconViews([DeconView(mvd_exe, img, weight, PSF_kernel, PSF_type, blockSize, 1, filterBlocksForContent)
                              for img, weight in izip(images, mvd_weights)],
                             exe)
    decon = MultiViewDeconvolutionSeq(decon_views, n_iterations, PsiInitBlurredFusedFactory(), cptf, factory)
    if not decon.initWasSuccessful():
      printFn("Something went wrong initializing MultiViewDeconvolution")
      return None
    else:
      decon.runIterations()
      return decon.getPSI()
  finally:
    # Only shut down the thread pool if it was created here
    if not exe:
      mvd_exe.shutdownNow()

