#
#   Module for MFCC extraction
#   By Maigo Yun Wang, 02/08/2012
#
#   Quick tutorial:
#       import MFCC
# x = ...         # x is a wave signal saved in a 1-D numpy array
#       mfcc = MFCC.extract(x)
# mfcc is a 2-D numpy array, where each row is the
# MFCC of a frame in x
#       mfcc = MFCC.extract(x, show = True)
# This will also plot the MFCC and the spectrogram
# reconstructed from MFCC by inverse DCT
#
#   Feel free to customize the parameters on Lines 67 - 79.
#

from numpy import *
from numpy.linalg import *
# from constants import *


def hamming(n):
    """
    Generate a hamming window of n points as a numpy array.
    """
    return 0.54 - 0.46 * cos(2 * pi / n * (arange(n) + 0.5))


def melfb(p, n, fs):
    """
    Return a Mel filterbank matrix as a numpy array.
    Inputs:
        p:  number of filters in the filterbank
        n:  length of fft
        fs: sample rate in Hz
    Ref. http://www.ifp.illinois.edu/~minhdo/teaching/speaker_recognition/code/melfb.m
    """
    f0 = 700.0 / fs
    fn2 = int(floor(n / 2))
    lr = log(1 + 0.5 / f0) / (p + 1)
    CF = fs * f0 * (exp(arange(1, p + 1) * lr) - 1)
    bl = n * f0 * (exp(array([0, 1, p, p + 1]) * lr) - 1)
    b1 = int(floor(bl[0])) + 1
    b2 = int(ceil(bl[1]))
    b3 = int(floor(bl[2]))
    b4 = min(fn2, int(ceil(bl[3]))) - 1
    pf = log(1 + arange(b1, b4 + 1) / f0 / n) / lr
    fp = floor(pf)
    pm = pf - fp
    M = zeros((p, 1 + fn2))
    for c in range(b2 - 1, b4):
        r = fp[c] - 1
        M[r, c + 1] += 2 * (1 - pm[c])
    for c in range(b3):
        r = fp[c]
        M[r, c + 1] += 2 * pm[c]
    return M, CF


def dctmtx(n):
    """
    Return the DCT-II matrix of order n as a numpy array.
    """
    x, y = meshgrid(range(n), range(n))
    D = sqrt(2.0 / n) * cos(pi * (2 * x + 1) * y / (2 * n))
    D[0] /= sqrt(2)
    return D

"""
Parameters
"""
# FRAME_LEN = int(0.02 * FS)              # Frame length
# FRAME_SHIFT = int(0.01 * FS)            # Frame shift
# FFT_SIZE = 2048                         # How many points for FFT
# WINDOW = hamming(FRAME_LEN)             # Window function
FS = 8000                                   # Sampling rate

# ---Frame length and frame shift calculation---
frm_len = 1024                              # Frame length e.g. 512 samples or 64ms
no_of_sec = float(frm_len) / FS             # No of seconds in a sample
FRAME_LEN = int(no_of_sec * FS)
FRAME_SHIFT = int((no_of_sec / 2) * FS)     # Frame shift = 50% overlap
WINDOW = hamming(FRAME_LEN)                 # Window function


# MFCC Parameters
FFT_SIZE = 1024                         # How many points for FFT
PRE_EMPH = 0.95                         # Pre-emphasis factor
BANDS = 40                              # Number of Mel filters
COEFS = 13                              # Number of Mel cepstra coefficients to keep
POWER_SPECTRUM_FLOOR = 1e-100           # Flooring for the power to avoid log(0)

# The Mel filterbank matrix and the center frequencies of each band
M, CF = melfb(BANDS, FFT_SIZE, FS)
D = dctmtx(BANDS)[1:COEFS + 1]
# The DCT matrix. Change the index to [0:COEFS] if you want to keep the 0-th coefficient
invD = inv(dctmtx(BANDS))[:, 1:COEFS + 1]
# The inverse DCT matrix. Change the index to [0:COEFS] if you want to
# keep the 0-th coefficient


def extract(frame):
    """
    Extract MFCC coefficients of a sound frame x
    """

    # Pre-emphasis
    frame[1:] -= frame[:-1] * PRE_EMPH
    # Power spectrum
    X = abs(fft.fft(frame, FFT_SIZE)[:FFT_SIZE / 2 + 1]) ** 2
    X[X < POWER_SPECTRUM_FLOOR] = POWER_SPECTRUM_FLOOR  # Avoid zero
    # Mel filtering, logarithm, DCT
    X = dot(D, log(dot(M, X)))
    frame_features = X

    return frame_features
