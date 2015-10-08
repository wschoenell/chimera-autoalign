from __future__ import division

import numpy as np
from donut.don11 import Donut

from chimera.core.chimeraobject import ChimeraObject
from chimera.core.lock import lock
from chimera.core.exceptions import ChimeraException, ClassLoaderException
from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY
from chimera.interfaces.autofocus import Autofocus as IAutofocus
from chimera.interfaces.autofocus import StarNotFoundException, FocusNotFoundException
from chimera.interfaces.focuser import InvalidFocusPositionException
from chimera.controllers.imageserver.imagerequest import ImageRequest
from chimera.controllers.imageserver.util         import getImageServer
from chimera.util.image import Image
from chimera.util.output import red, green

from chimera_autoalign.util.mkoptics import MkOptics,HexapodAxes
from astropy import units
from collections import OrderedDict
from astropy.coordinates import Angle

plot = True
try:
    import pylab as P
except (ImportError, RuntimeError, ClassLoaderException):
    plot = False

from math import sqrt
import time
import os
import logging

class StarDistributionException(ChimeraException):
    pass

class AutoAlignException(ChimeraException):
    pass

class AutoAlign(ChimeraObject,IAutofocus):

    """
    Auto Align
    ============

    This controller is a subclass of AutoFocus. It will try to characterizes the current system
    by measuring the zernike coefficients of out of focus stellar images and determine the
    corrections to hexapod positions for focus (Z), coma (X and Y) and astigmatism (U and V).

    1) take exposure to find focus star.

    2) set window and binning if necessary and start iteration:

       Get n points starting at min_pos and ending at max_pos focus positions,
       and for each position measure FWHM of a target star (currently the
       brighter star in the field).

       Fit a parabola to the FWHM points measured.

    3) Leave focuser at best focus point (parabola vertice)

    """

    __config__ = {'cfp' : None , # Comma free point in mm
                  'd' : 0.83, # Telescope apperture Diameter in m
                  'eps': 0.42, # Size of central obscuration in m
                  'alambda': 0.800, # wavelenght of observation in micron (usually non-critical)
                  'pixel': 0.55, # Pixel scale
                  'ngrid': 64, # half-size of computing grid, 64 recommended
                  'thresh' : 0.05, # relative threshould for fitted intensities
                  'nzer': 8, # number of fitted Zernike terms
                  'mpi_threads': 8,
                  'mpi_use': True,
                  }

    def __init__(self):
        ChimeraObject.__init__(self)

        self.imageRequest = None
        self.filter = None
        self.currentRun = None

    def getCam(self):
        return self.getManager().getProxy(self["camera"])

    def getFilter(self):
        return self.getManager().getProxy(self["filterwheel"])

    def getFocuser(self):
        return self.getManager().getProxy(self["focuser"])

    def _getID(self):
        return "autoalign-%s" % time.strftime("%Y%m%d-%H%M%S")

    @lock
    def align(self, filter=None, exptime=None, binning=None, window=None,
              intra=True, check_stellar_ditribution=True,minimum_star=100,niter=10):

        self.currentRun = self._getID()

        # self.log.debug("="*40)
        # self.log.debug("[%s] Starting autoalign run." % time.strftime("%c"))
        # self.log.debug("="*40)
        # self.log.debug("Focus offset: %f" % (focus))

        self.imageRequest = ImageRequest()
        self.imageRequest["exptime"] = exptime or 10
        self.imageRequest["frames"] = 1
        self.imageRequest["shutter"] = "OPEN"

        if filter:
            self.filter = filter
            self.log.debug("Using filter %s." % self.filter)
        else:
            self.filter = False
            self.log.debug("Using current filter.")

        if binning:
            self.imageRequest["binning"] = binning

        if window:
            self.imageRequest["window"] = window

        # Sets up order and threshould
        alignOrder = OrderedDict([('comma',0.009*units.mm),
                                  ('astigmatism',10.*units.arcsec)])

        done = False
        iter = 0
        hexapod_offset = HexapodAxes()

        while not done:
            # 1. Take an image to work on
            image = self._takeImage()

            # 2. Make a catalog of sources
            cat = self._findStars(image)
            if len(cat) < minimum_star and minimum_star > 0:
                raise StarNotFoundException("Could not find the required number of stars. Found %i of %i."%(len(cat),
                                                                                                            minimum_star))

            if check_stellar_ditribution:
                self.checkDistribution(cat)

            # 3. Calculate hexapod offsets
            mkopt = MkOptics()

            mkopt.setCFP(self["cfp"]*units.mm)
            mkopt.setPixScale(np.float(image['CCDPSZX'])*units.micron)
            mkopt.setMPIThreads(self["mpi_threads"])

            hexapod_offset = mkopt.align(image.filename)
            applied = False

            for current_aberration_name in alignOrder:

                apply_offset = hexapod_offset[current_aberration_name]
                # 3.1 check if offset on current aberration been corrected is higher than threshold and apply correction
                for ab_key in apply_offset:
                    if np.abs(apply_offset[ab_key]) > alignOrder[current_aberration_name]:
                        self._applyHexapodOffset(ab_key,apply_offset[ab_key])
                        applied = True

                if applied:
                    break

            if not applied:
                # Break if no correction was applied
                break

            if iter > niter:
                self.log.warning('Maximum number of iterations reached.')
                break

            self.stepComplete(hexapod_offset,None,image)

            iter += 1

        # Apply focus and return
        self._applyHexapodOffset('Z',hexapod_offset.z)

        return hexapod_offset

    def checkDistribution(self,catalog):
        '''
        Separate CCD in 8 quadrants and check that are stars on all of them.

        :param catalog:
        :return:
        '''
        camera = self.getCam()
        width,heigth = camera.getPixelSize()
        ngrid = 8
        wgrid = np.linspace(0, width,ngrid)
        hgrid = np.linspace(0,heigth,ngrid)

        star_per_grid = np.zeros((ngrid-1,ngrid-1))

        for i in range(ngrid-1):
            mask_w = np.bitwise_and(catalog['X_IMAGE'] > wgrid[i],
                                    catalog['X_IMAGE'] < wgrid[i+1])
            for j in range(ngrid-1):
                mask_h = np.bitwise_and(catalog['Y_IMAGE'] > hgrid[i],
                                        catalog['Y_IMAGE'] < hgrid[i+1])
                mask = np.bitwise_and(mask_w,
                                      mask_h)

                star_per_grid[i][j] += np.sum(mask)

        nstar = len(catalog)/2/ngrid**2

        mask_starpg = star_per_grid < nstar

        if np.any(mask_starpg):
            raise StarDistributionException('Stellar distribution not suitable for optical alignment.')


    def _takeImage(self):

        self.imageRequest["filename"] = os.path.join(SYSTEM_CONFIG_DIRECTORY, self.currentRun, "align.fits")

        cam = self.getCam()

        if self.filter:
            filter = self.getFilter()
            filter.setFilter(self.filter)

        frame = cam.expose(self.imageRequest)

        if frame:
            return frame[0]
        else:
            raise Exception("Error taking image.")

    def _findStars(self, frame):

        config = {}
        config['PIXEL_SCALE'] = self['pixel']
        config['BACK_TYPE'] = "AUTO"

        # CCD saturation level in ADUs.
        s = self.getCam()["ccd_saturation_level"]
        if s is not None:  # If there is no ccd_saturation_level on the config, use the default.
            config['SATUR_LEVEL'] = s

        # no output, please
        config['VERBOSE_TYPE'] = "QUIET"

        config['DETECT_MINAREA'] = 200
        config['DETECT_THRESH'] = 10.0
        config['PARAMETERS_LIST'] = ["NUMBER",'X_IMAGE', 'Y_IMAGE',
                                         "XWIN_IMAGE", "YWIN_IMAGE",
                                         "FLUX_BEST", "FWHM_IMAGE",
                                         "FLAGS"]

        catalogName = os.path.splitext(frame.filename())[0] + ".catalog"
        configName = os.path.splitext(frame.filename())[0] + ".config"
        return frame.extract(config, saveCatalog=catalogName, saveConfig=configName)

    def _applyHexapodOffset(self,axis,offset):

        focuser = self.getFocuser()

        if axis.upper() in ['X','Y','Z']:
            # move should be in steps
            if offset.value > 0.:
                focuser.moveOut(np.abs(offset.to(units.mm).value)/focuser["step"],
                                axis.upper())
            else:
                focuser.moveIn(np.abs(offset.to(units.mm).value)/focuser["step"],
                               axis.upper())
        elif axis.upper() in ['U','V']:
            # move should be in degrees
            if offset.value > 0.:
                focuser.moveOut(np.abs(offset.to(units.degrees).value)/focuser["step"],
                                axis.upper())
            else:
                focuser.moveIn(np.abs(offset.to(units.degrees).value)/focuser["step"],
                               axis.upper())
