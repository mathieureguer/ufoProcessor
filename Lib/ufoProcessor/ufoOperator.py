import os
import glob
import functools

import random
import defcon
from warnings import warn
import collections
import logging, traceback

from fontTools.designspaceLib import DesignSpaceDocument, SourceDescriptor, InstanceDescriptor, AxisDescriptor, RuleDescriptor, processRules
from fontTools.designspaceLib.split import splitInterpolable
from fontTools.ufoLib import fontInfoAttributesVersion1, fontInfoAttributesVersion2, fontInfoAttributesVersion3
from fontTools.misc import plistlib

from fontMath.mathGlyph import MathGlyph
from fontMath.mathInfo import MathInfo
from fontMath.mathKerning import MathKerning
from mutatorMath.objects.mutator import buildMutator
from mutatorMath.objects.location import Location

import fontParts.fontshell.font

import ufoProcessor.varModels
import ufoProcessor.pens
from ufoProcessor.varModels import VariationModelMutator
from ufoProcessor.pens import checkGlyphIsEmpty, DecomposePointPen
from ufoProcessor.logger import Logger

_memoizeCache = dict()
_memoizeStats = dict()

def ip(a, b, f):
    return a+f*(b-a)

def immutify(obj):
    # make an immutable version of this object. 
    # assert immutify(10) == (10,)
    # assert immutify([10, 20, "a"]) == (10, 20, 'a')
    # assert immutify(dict(foo="bar", world=["a", "b"])) == ('foo', ('bar',), 'world', ('a', 'b'))
    hashValues = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            hashValues.extend([key, immutify(value)])
    elif isinstance(obj, list):
        for value in obj:
            hashValues.extend(immutify(value))
    else:
        hashValues.append(obj)
    return tuple(hashValues)

def memoize(function):
    @functools.wraps(function)
    def wrapper(self, *args, **kwargs):        
        immutableargs = tuple([immutify(a) for a in args])
        immutablekwargs = immutify(kwargs)
        key = (function.__name__, self, immutableargs, immutify(kwargs))
        if key in _memoizeCache:
            if not key in _memoizeStats:
                _memoizeStats[key] = 0
            _memoizeStats[key] += 1
            return _memoizeCache[key]
        else:
            result = function(self, *args, **kwargs)
            _memoizeCache[key] = result
            return result
    return wrapper

def inspectMemoizeCache():
    functionNames = []
    stats = {}
    for k in _memoizeCache.keys():
        functionName = k[0]
        if not functionName in stats:
            stats[functionName] = 0
        stats[functionName] += 1
    print(stats)

def getUFOVersion(ufoPath):
    # Peek into a ufo to read its format version.
            # <?xml version="1.0" encoding="UTF-8"?>
            # <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            # <plist version="1.0">
            # <dict>
            #   <key>creator</key>
            #   <string>org.robofab.ufoLib</string>
            #   <key>formatVersion</key>
            #   <integer>2</integer>
            # </dict>
            # </plist>
    metaInfoPath = os.path.join(ufoPath, "metainfo.plist")
    with open(metaInfoPath, 'rb') as f:
        p = plistlib.load(f)
        return p.get('formatVersion')

def getDefaultLayerName(f):
    # get the name of the default layer from a defcon font (outside RF) and from a fontparts font (outside and inside RF)
    if issubclass(type(f), defcon.objects.font.Font):
        return f.layers.defaultLayer.name
    elif issubclass(type(f), fontParts.fontshell.font.RFont):
        return f.defaultLayer.name
    return None

# wrapped, not inherited, as Just says.
class UFOOperator(object):
    
    fontClass = defcon.Font
    layerClass = defcon.Layer
    glyphClass = defcon.Glyph
    libClass = defcon.Lib
    glyphContourClass = defcon.Contour
    glyphPointClass = defcon.Point
    glyphComponentClass = defcon.Component
    glyphAnchorClass = defcon.Anchor
    kerningClass = defcon.Kerning
    groupsClass = defcon.Groups
    infoClass = defcon.Info
    featuresClass = defcon.Features

    mathInfoClass = MathInfo
    mathGlyphClass = MathGlyph
    mathKerningClass = MathKerning

    def __init__(self, pathOrObject=None, ufoVersion=3, useVarlib=True, debug =False):
        self.ufoVersion = ufoVersion
        self.useVarlib = useVarlib
        self._fontsLoaded = False
        self.fonts = {}
        self.roundGeometry = False
        self.mutedAxisNames = None    # list of axisname that need to be muted
        self.debug = debug
        self.logger = None    
        if pathOrObject is None:
            self.doc = DesignSpaceDocument()
        elif isinstance(pathOrObject, str):
            self.doc = DesignSpaceDocument()
            self.doc.read(pathOrObject)
        else:
            # XX test this
            self.doc = pathOrObject

        if self.debug:
            docBaseName = os.path.splitext(self.doc.path)[0]
            logPath = f"{docBaseName}_log.txt"
            self.logger = Logger(path=logPath, rootDirectory=None)
            self.logger.time()
            self.logger.info(f"## {self.doc.path}")
            self.logger.info(f"\tUFO version: {self.ufoVersion}")
            self.logger.info(f"\tround Geometry: {self.roundGeometry}")
            if self.useVarlib:
                self.logger.info(f"\tinterpolating with varlib")
            else:
                self.logger.info(f"\tinterpolating with mutatorMath")

    def _instantiateFont(self, path):
        """ Return a instance of a font object with all the given subclasses"""
        try:
            return self.fontClass(path,
                layerClass=self.layerClass,
                libClass=self.libClass,
                kerningClass=self.kerningClass,
                groupsClass=self.groupsClass,
                infoClass=self.infoClass,
                featuresClass=self.featuresClass,
                glyphClass=self.glyphClass,
                glyphContourClass=self.glyphContourClass,
                glyphPointClass=self.glyphPointClass,
                glyphComponentClass=self.glyphComponentClass,
                glyphAnchorClass=self.glyphAnchorClass)
        except TypeError:
            # if our fontClass doesnt support all the additional classes
            return self.fontClass(path)
    
    # UFOProcessor compatibility
    # not sure whether to expose all the DesignSpaceDocument internals here
    # One can just use ufoOperator.doc to get it going?
    # Let's see how difficilt it is

    def read(self, path):
        """Wrap a DesignSpaceDocument"""
        self.doc = DesignSpaceDocument()
        self.doc.read(path)
        self.changed()

    def write(self, path):
        """Write the wrapped DesignSpaceDocument"""
        self.doc.write(path)

    def addAxis(self, axisDescriptor):
        self.doc.addAxis(axisDescriptor)

    def addSource(self, sourceDescriptor):
        self.doc.addSource(sourceDescriptor)

    def addInstance(self, instanceDescriptor):
        self.doc.addInstance(instanceDescriptor)

    @property
    def lib(self):
        if self.doc is not None:
            return self.doc.lib
        return None # return dict() maybe?

    @property
    def axes(self):
        if self.doc is not None:
            return self.doc.axes
        return []

    @property
    def sources(self):
        if self.doc is not None:
            return self.doc.sources
        return []

    @property
    def instances(self):
        if self.doc is not None:
            return self.doc.instances
        return []

    @property
    def formatVersion(self):
        if self.doc is not None:
            return self.doc.formatVersion
        return []

    @formatVersion.setter
    def formatVersion(self, value):
        if self.doc is not None:
            self.doc.formatVersion = value

    # loading and updating fonts
    def loadFonts(self, reload=False):
        # Load the fonts and find the default candidate based on the info flag
        if self._fontsLoaded and not reload:
            if self.debug:
                self.logger.info("\t\t-- loadFonts requested, but fonts are loaded already and no reload requested")
            return
        names = set()
        actions = []
        if self.debug:
            self.logger.info("## loadFonts")
        for i, sourceDescriptor in enumerate(self.doc.sources):
            if sourceDescriptor.name is None:
                # make sure it has a unique name
                sourceDescriptor.name = "source.%d" % i
            if sourceDescriptor.name not in self.fonts:
                if os.path.exists(sourceDescriptor.path):
                    f = self.fonts[sourceDescriptor.name] = self._instantiateFont(sourceDescriptor.path)
                    thisLayerName = getDefaultLayerName(f)
                    actions.append(f"loaded: {os.path.basename(sourceDescriptor.path)}, layer: {thisLayerName}, format: {getUFOVersion(sourceDescriptor.path)}, id: {id(f):X}")
                    names |= set(self.fonts[sourceDescriptor.name].keys())
                else:
                    self.fonts[sourceDescriptor.name] = None
                    actions.append("source ufo not found at %s" % (sourceDescriptor.path))
        self.glyphNames = list(names)
        if self.debug:
            for item in actions:
                self.logger.infoItem(item)
        self._fontsLoaded = True

    def _logLoadedFonts(self):
        # dump info about the loaded fonts to the log
        items = []
        self.logger.info("\t# font status:")
        for name, fontObj in self.fonts.items():
            self.logger.info(f"\t\tloaded: , id: {id(fontObj):X}, {os.path.basename(fontObj.path)}, format: {getUFOVersion(fontObj.path)}")

    def updateFonts(self, fontObjects):
        # this is to update the loaded fonts. 
        # it should be the way for an editor to provide a list of fonts that are open
        #self.fonts[sourceDescriptor.name] = None
        hasUpdated = False
        for newFont in fontObjects:
            for fontName, haveFont in self.fonts.items():
                if haveFont.path == newFont.path and id(haveFont)!=id(newFont):
                    note = f"## updating source {self.fonts[fontName]} with {newFont}"
                    if self.debug:
                        self.logger.time()
                        self.logger.info(note)
                    self.fonts[fontName] = newFont
                    hasUpdated = True
        if hasUpdated:
            self.changed()

    # caching
    def changed(self):
        # clears everything relating to this designspacedocument
        # the cache could contain more designspacedocument objects.
        for key in list(_memoizeCache.keys()):
            if key[1] == self:
                del _memoizeCache[key]
        #_memoizeCache.clear()

    def glyphChanged(self, glyphName):
        # clears this one specific glyph from the memoize cache
        for key in list(_memoizeCache.keys()):            
            #print(f"glyphChanged {[(i,m) for i, m in enumerate(key)]} {glyphName}")
            # the glyphname is hiding quite deep in key[2]
            # (('glyphTwo',),)
            # this is because of how immutify does it. Could be different I suppose but this works
            if key[0] in ("getGlyphMutator", "collectSourcesForGlyph") and key[2][0][0] == glyphName:
                del _memoizeCache[key]
   
   # manipulate locations and axes
    def splitLocation(self, location):
        # split a location in a continouous and a discrete part
        discreteAxes = [a.name for a in self.getOrderedDiscreteAxes()]
        continuous = {}
        discrete = {}
        for name, value in location.items():
            if name in discreteAxes:
                discrete[name] = value
            else:
                continuous[name] = value
        if not discrete:
            return continuous, None
        return continuous, discrete

    def _serializeAnyAxis(self, axis):
        if hasattr(axis, "serialize"):
            return axis.serialize()
        else:
            if hasattr(axis, "values"):
                # discrete axis does not have serialize method, meh
                return dict(
                    tag=axis.tag,
                    name=axis.name,
                    labelNames=axis.labelNames,
                    minimum = min(axis.values), # XX is this allowed
                    maximum = max(axis.values), # XX is this allowed
                    values=axis.values,
                    default=axis.default,
                    hidden=axis.hidden,
                    map=axis.map,
                    axisOrdering=axis.axisOrdering,
                    axisLabels=axis.axisLabels,
                )

    def getSerializedAxes(self, discreteLocation=None):
        serialized = []
        for axis in self.getOrderedContinuousAxes():
            serialized.append(self._serializeAnyAxis(axis))
        return serialized

    def getContinuousAxesForMutator(self):
        # map the axis values?
        d = collections.OrderedDict()
        for axis in self.getOrderedContinuousAxes():
            d[axis.name] = self._serializeAnyAxis(axis)
        return d

    def _getAxisOrder(self):
        return [a.name for a in self.doc.axes]

    axisOrder = property(_getAxisOrder, doc="get the axis order from the axis descriptors")

    def getOrderedDiscreteAxes(self):
        # return the list of discrete axis objects, in the right order
        axes = []
        for axisName in self.doc.getAxisOrder():
            axisObj = self.doc.getAxis(axisName)
            if hasattr(axisObj, "values"):
                axes.append(axisObj)
        return axes

    def getOrderedContinuousAxes(self):
        # return the list of continuous axis objects, in the right order
        axes = []
        for axisName in self.doc.getAxisOrder():
            axisObj = self.doc.getAxis(axisName)
            if not hasattr(axisObj, "values"):
                axes.append(axisObj)
        return axes

    def checkDiscreteAxisValues(self, location):
        # check if the discrete values in this location are allowed
        for discreteAxis in self.getOrderedDiscreteAxes():
            testValue = location.get(discreteAxis.name)
            if not testValue in discreteAxis.values:
                return False
        return True

    def collectBaseGlyphs(self, glyphName, location):
        # make a list of all baseglyphs needed to build this glyph, at this location
        # Note: different discrete values mean that the glyph component set up can be different too
        continuousLocation, discreteLocation = self.splitLocation(location)
        names = set()
        def _getComponentNames(glyph):
            # so we can do recursion
            names = set()
            for comp in glyph.components:
                names.add(comp.baseGlyph)
                for n in _getComponentNames(glyph.font[comp.baseGlyph]):
                    names.add(n)
            return list(names)
        for sourceDescriptor in self.findSourceDescriptorsForDiscreteLocation(discreteLocation):
            sourceFont = self.fonts[sourceDescriptor.name]
            if not glyphName in sourceFont: continue
            [names.add(n) for n in _getComponentNames(sourceFont[glyphName])]
        return list(names)

    @memoize
    def findSourceDescriptorsForDiscreteLocation(self, discreteLocDict=None):
        # return a list of all sourcedescriptors that share the values in the discrete loc tuple
        # so this includes all sourcedescriptors that point to layers
        # discreteLocDict {'countedItems': 1.0, 'outlined': 0.0}, {'countedItems': 1.0, 'outlined': 1.0}
        sources = []
        for s in self.doc.sources:
            ok = True
            if discreteLocDict is None:
                sources.append(s)
                continue
            for name, value in discreteLocDict.items():
                if name in s.location:
                    if s.location[name] != value:
                        ok = False
                else:
                    ok = False
                    continue
            if ok:
                sources.append(s)
        return sources

    def getVariationModel(self, items, axes, bias=None):
        # Return either a mutatorMath or a varlib.model object for calculating.
        if self.useVarlib:
            # use the varlib variation model
            try:
                return dict(), VariationModelMutator(items, axes=self.doc.axes, extrapolate=True)
            except TypeError:
                if self.debug:
                    error = traceback.format_exc()
                    note = "Error while making VariationModelMutator for {loc}:\n{error}"
                    self.logger.info(note)
                return {}, None
            except (KeyError, AssertionError):
                if self.debug:
                    error = traceback.format_exc()
                    note = "UFOProcessor.getVariationModel error: {error}"
                    self.logger.info(note)
                return {}, None
        else:
            # use mutatormath model
            axesForMutator = self.getContinuousAxesForMutator()
            # mutator will be confused by discrete axis values.
            # the bias needs to be for the continuous axes only
            biasForMutator, _ = self.splitLocation(bias)
            return buildMutator(items, axes=axesForMutator, bias=biasForMutator)
        return {}, None

    @memoize
    def newDefaultLocation(self, bend=False, discreteLocation=None):
        # overwrite from fontTools.newDefaultLocation
        # we do not want this default location always to be mapped.
        loc = collections.OrderedDict()
        for axisDescriptor in self.doc.axes:
            axisName = axisDescriptor.name
            axisValue = axisDescriptor.default
            if discreteLocation is not None:
                # if we want to find the default for a specific discreteLoation
                # we can not use the discrete axis' default value
                # -> we have to use the value in the given discreteLocation
                if axisDescriptor.name in discreteLocation:
                    axisValue = discreteLocation[axisDescriptor.name]
            else:
                axisValue = axisDescriptor.default
            if bend:
                loc[axisName] = axisDescriptor.map_forward(
                    axisValue
                )
            else:
                loc[axisName] = axisValue
        return loc

    @memoize
    def isAnisotropic(self, location):
        # check if the location has anisotropic values
        for v in location.values():
            if isinstance(v, (list, tuple)):
                return True
        return False

    @memoize
    def splitAnisotropic(self, location):
        # split the anisotropic location into a horizontal and vertical component
        x = Location()
        y = Location()
        for dim, val in location.items():
            if type(val)==tuple:
                x[dim] = val[0]
                y[dim] = val[1]
            else:
                x[dim] = y[dim] = val
        return x, y

    @memoize
    def _getAxisOrder(self):
        return [a.name for a in self.doc.axes]
    
    def generateUFOs(self, useVarLib=None):
        # generate an UFO for each of the instance locations
        previousModel = self.useVarlib
        if useVarLib is not None:
            self.useVarlib = useVarlib
        glyphCount = 0
        self.loadFonts()
        if self.debug:
            self.logger.info("## generateUFO")
        for loc, space in splitInterpolable(self.doc):
            spaceDoc = self.__class__(pathOrObject=space)
            if self.debug:
                self.logger.infoItem(f"Generating UFOs for continuous space at discrete location {loc}")
            v = 0
            for instanceDescriptor in self.doc.instances:
                if instanceDescriptor.path is None:
                    continue
                pairs = None
                bend = False
                font = self.makeInstance(instanceDescriptor,
                        processRules,
                        glyphNames=self.glyphNames,
                        pairs=pairs,
                        bend=bend,
                        )
                if self.debug:
                    self.logger.info(f"\t\t{os.path.basename(instanceDescriptor.path)}")
                instanceFolder = os.path.dirname(instanceDescriptor.path)
                if not os.path.exists(instanceFolder):
                    os.makedirs(instanceFolder)
                font.save(instanceDescriptor.path)
                glyphCount += len(font)
        if self.debug:
            self.logger.info(f"\t\tGenerated {glyphCount} glyphs altogether.")
        self.useVarlib = previousModel

    generateUFO = generateUFOs

    @memoize
    def getInfoMutator(self, discreteLocation=None):
        """ Returns a info mutator for this discrete location """
        infoItems = []
        if discreteLocation is not None:
            sources = self.findSourceDescriptorsForDiscreteLocation(discreteLocation)
        else:
            sources = self.doc.sources
        for sourceDescriptor in sources:
            if sourceDescriptor.layerName is not None:
                continue
            continuous, discrete = self.splitLocation(sourceDescriptor.location)
            loc = Location(continuous)
            sourceFont = self.fonts[sourceDescriptor.name]
            if sourceFont is None:
                continue
            if hasattr(sourceFont.info, "toMathInfo"):
                infoItems.append((loc, sourceFont.info.toMathInfo()))
            else:
                infoItems.append((loc, self.mathInfoClass(sourceFont.info)))
        infoBias = self.newDefaultLocation(bend=True, discreteLocation=discreteLocation)
        bias, self._infoMutator = self.getVariationModel(infoItems, axes=self.getSerializedAxes(), bias=infoBias)
        return self._infoMutator

    def collectForegroundLayerNames(self):
        """Return list of names of the default layers of all the fonts in this system. 
            Include None and foreground. XX Why
        """
        names = set([None, 'foreground'])
        for key, font in self.fonts.items():
            names.add(getDefaultLayerName(font))
        return list(names)

    @memoize
    def getKerningMutator(self, pairs=None, discreteLocation=None):
        """ Return a kerning mutator, collect the sources, build mathGlyphs.
            If no pairs are given: calculate the whole table.
            If pairs are given then query the sources for a value and make a mutator only with those values.
        """
        if discreteLocation is not None:
            sources = self.findSourceDescriptorsForDiscreteLocation(discreteLocation)
        else:
            sources = self.sources
        kerningItems = []
        foregroundLayers = self.collectForegroundLayerNames()
        if pairs is None:
            for sourceDescriptor in sources:
                if sourceDescriptor.layerName not in foregroundLayers:
                    continue
                if not sourceDescriptor.muteKerning:
                    # filter this XX @@
                    continuous, discrete = self.splitLocation(sourceDescriptor.location)
                    loc = Location(continuous)
                    sourceFont = self.fonts[sourceDescriptor.name]
                    if sourceFont is None: continue
                    # this makes assumptions about the groups of all sources being the same.
                    kerningItems.append((loc, self.mathKerningClass(sourceFont.kerning, sourceFont.groups)))
        else:
            self._kerningMutatorPairs = pairs
            for sourceDescriptor in sources:
                # XXX check sourceDescriptor layerName, only foreground should contribute
                if sourceDescriptor.layerName is not None:
                    continue
                if not os.path.exists(sourceDescriptor.path):
                    continue
                if not sourceDescriptor.muteKerning:
                    sourceFont = self.fonts[sourceDescriptor.name]
                    if sourceFont is None:
                        continue
                    continuous, discrete = self.splitLocation(sourceDescriptor.location)
                    loc = Location(continuous)
                    # XXX can we get the kern value from the fontparts kerning object?
                    kerningItem = self.mathKerningClass(sourceFont.kerning, sourceFont.groups)
                    if kerningItem is not None:
                        sparseKerning = {}
                        for pair in pairs:
                            v = kerningItem.get(pair)
                            if v is not None:
                                sparseKerning[pair] = v
                        kerningItems.append((loc, self.mathKerningClass(sparseKerning)))
        kerningBias = self.newDefaultLocation(bend=True, discreteLocation=discreteLocation)
        bias, thing = self.getVariationModel(kerningItems, axes=self.getSerializedAxes(), bias=kerningBias) #xx
        bias, self._kerningMutator = self.getVariationModel(kerningItems, axes=self.getSerializedAxes(), bias=kerningBias)
        return self._kerningMutator

    @memoize
    def getGlyphMutator(self, glyphName,
            decomposeComponents=False,
            **discreteLocation,  
            ):
        """make a mutator / varlib object for glyphName, with the sources for the given discrete location"""
        items, unicodes = self.collectSourcesForGlyph(glyphName, decomposeComponents=decomposeComponents, **discreteLocation)
        new = []
        for a, b, c in items:
            if hasattr(b, "toMathGlyph"):
                # note: calling toMathGlyph ignores the mathGlyphClass preference
                # maybe the self.mathGlyphClass is not necessary?
                new.append((a,b.toMathGlyph()))
            else:
                new.append((a,self.mathGlyphClass(b)))
        thing = None
        thisBias = self.newDefaultLocation(bend=True, discreteLocation=discreteLocation)
        try:
            bias, thing = self.getVariationModel(new, axes=self.getSerializedAxes(), bias=thisBias) #xx
        except:
            error = traceback.format_exc()
            note = f"Error in getGlyphMutator for {glyphName}:\n{error}"
            if self.debug:
                self.logger.info(note)
        return thing, unicodes

    # stats indicate this does not get called very often, so caching may not be useful
    #@memoize
    def isLocalDefault(self, location):
        # return True if location is a local default
        defaults = {}
        for aD in self.doc.axes:
            defaults[aD.name] = aD.default
        for axisName, value in location.items():
            if defaults[axisName] != value:
                return False
        return True

    # stats indicate this does not get called very often, so caching may not be useful
    #@memoize
    def filterThisLocation(self, location, mutedAxes=None):
        # return location with axes is mutedAxes removed
        # this means checking if the location is a non-default value
        if not mutedAxes:
            return False, location
        defaults = {}
        ignoreSource = False
        for aD in self.doc.axes:
            defaults[aD.name] = aD.default
        new = {}
        new.update(location)
        for mutedAxisName in mutedAxes:
            if mutedAxisName not in location:
                continue
            if mutedAxisName not in defaults:
                continue
            if location[mutedAxisName] != defaults.get(mutedAxisName):
                ignoreSource = True
            del new[mutedAxisName]
        return ignoreSource, new

    @memoize
    def collectSourcesForGlyph(self, glyphName, decomposeComponents=False, discreteLocation=None):
        """ Return a glyph mutator
            decomposeComponents = True causes the source glyphs to be decomposed first
            before building the mutator. That gives you instances that do not depend
            on a complete font. If you're calculating previews for instance.

            findSourceDescriptorsForDiscreteLocation returns sources from layers as well
        """
        items = []
        empties = []
        foundEmpty = False
        # is bend=True necessary here?
        defaultLocation = self.newDefaultLocation(bend=True, discreteLocation=discreteLocation)
        # 
        if discreteLocation is not None:
            sources = self.findSourceDescriptorsForDiscreteLocation(discreteLocation)
        else:
            sources = self.doc.sources
        unicodes = set()       # unicodes for this glyph
        for sourceDescriptor in sources:
            if not os.path.exists(sourceDescriptor.path):
                #kthxbai
                note = "\tMissing UFO at %s" % sourceDescriptor.path
                if self.debug:
                    self.logger.info(note)
                continue
            if glyphName in sourceDescriptor.mutedGlyphNames:
                self.logger.info(f"\t\tglyphName {glyphName} is muted")
                continue
            thisIsDefault = self.isLocalDefault(sourceDescriptor.location)
            ignoreSource, filteredLocation = self.filterThisLocation(sourceDescriptor.location, self.mutedAxisNames)
            if ignoreSource:
                continue
            f = self.fonts.get(sourceDescriptor.name)
            if f is None: continue
            loc = Location(sourceDescriptor.location)
            sourceLayer = f
            if not glyphName in f:
                # log this>
                continue
            layerName = getDefaultLayerName(f)
            sourceGlyphObject = None
            # handle source layers
            if sourceDescriptor.layerName is not None:
                # start looking for a layer
                # Do not bother for mutatorMath designspaces
                layerName = sourceDescriptor.layerName
                sourceLayer = getLayer(f, sourceDescriptor.layerName)
                if sourceLayer is None:
                    continue
                if glyphName not in sourceLayer:
                    # start looking for a glyph
                    # this might be a support in a sparse layer
                    # so we're skipping!
                    continue
            # still have to check if the sourcelayer glyph is empty
            if not glyphName in sourceLayer:
                continue
            else:
                sourceGlyphObject = sourceLayer[glyphName]
                if sourceGlyphObject.unicodes is not None:
                    for u in sourceGlyphObject.unicodes:
                        unicodes.add(u)
                if checkGlyphIsEmpty(sourceGlyphObject, allowWhiteSpace=True):
                    foundEmpty = True
                    #sourceGlyphObject = None
                    #continue
            if decomposeComponents:
                # what about decomposing glyphs in a partial font?
                temp = self.glyphClass()
                p = temp.getPointPen()
                dpp = DecomposePointPen(sourceLayer, p)
                sourceGlyphObject.drawPoints(dpp)
                temp.width = sourceGlyphObject.width
                temp.name = sourceGlyphObject.name
                processThis = temp
            else:
                processThis = sourceGlyphObject
            sourceInfo = dict(source=f.path, glyphName=glyphName,
                    layerName=layerName,
                    location=filteredLocation,  #   sourceDescriptor.location,
                    sourceName=sourceDescriptor.name,
                    )
            if hasattr(processThis, "toMathGlyph"):
                processThis = processThis.toMathGlyph()
            else:
                processThis = self.mathGlyphClass(processThis)
            continuous, discrete = self.splitLocation(loc)
            items.append((continuous, processThis, sourceInfo))
            empties.append((thisIsDefault, foundEmpty))
        # check the empties:
        # if the default glyph is empty, then all must be empty
        # if the default glyph is not empty then none can be empty
        checkedItems = []
        emptiesAllowed = False
        # first check if the default is empty.
        # remember that the sources can be in any order
        for i, p in enumerate(empties):
            isDefault, isEmpty = p
            if isDefault and isEmpty:
                emptiesAllowed = True
                # now we know what to look for
        if not emptiesAllowed:
            for i, p in enumerate(empties):
                isDefault, isEmpty = p
                if not isEmpty:
                    checkedItems.append(items[i])
        else:
            for i, p in enumerate(empties):
                isDefault, isEmpty = p
                if isEmpty:
                    checkedItems.append(items[i])
        return checkedItems, unicodes

    collectMastersForGlyph = collectSourcesForGlyph

    def makeInstance(self, instanceDescriptor,
            doRules=None,
            glyphNames=None,
            pairs=None,
            bend=False):
        """ Generate a font object for this instance """
        if doRules is not None:
            warn('The doRules argument in DesignSpaceProcessor.makeInstance() is deprecated', DeprecationWarning, stacklevel=2)
        continuousLocation, discreteLocation = self.splitLocation(instanceDescriptor.location)

        font = self._instantiateFont(None)

        loc = Location(continuousLocation)
        anisotropic = False
        locHorizontal = locVertical = loc
        if self.isAnisotropic(loc):
            anisotropic = True
            locHorizontal, locVertical = self.splitAnisotropic(loc)
            if self.debug:
                self.logger.info(f"\t\t\tAnisotropic location for {instanceDescriptor.name}\n\t\t\t{instanceDescriptor.location}")
        if instanceDescriptor.kerning:
            if pairs:
                try:
                    kerningMutator = self.getKerningMutator(pairs=pairs, discreteLocation=discreteLocation)
                    kerningObject = kerningMutator.makeInstance(locHorizontal, bend=bend)
                    kerningObject.extractKerning(font)
                except:
                    error = traceback.format_exc()
                    note = f"makeInstance: Could not make kerning for {loc}\n{error}"
                    if self.debug:
                        self.logger.info(note)
            else:
                kerningMutator = self.getKerningMutator(discreteLocation=discreteLocation)
                if kerningMutator is not None:
                    kerningObject = kerningMutator.makeInstance(locHorizontal, bend=bend)
                    kerningObject.extractKerning(font)
                    if self.debug:
                        self.logger.info(f"\t\t\t{len(font.kerning)} kerning pairs added")

        # # make the info
        infoMutator = self.getInfoMutator(discreteLocation=discreteLocation)
        if infoMutator is not None:
            if not anisotropic:
                infoInstanceObject = infoMutator.makeInstance(loc, bend=bend)
            else:
                horizontalInfoInstanceObject = infoMutator.makeInstance(locHorizontal, bend=bend)
                verticalInfoInstanceObject = infoMutator.makeInstance(locVertical, bend=bend)
                # merge them again
                infoInstanceObject = (1,0)*horizontalInfoInstanceObject + (0,1)*verticalInfoInstanceObject
            if self.roundGeometry:
                infoInstanceObject = infoInstanceObject.round()
            infoInstanceObject.extractInfo(font.info)
        font.info.familyName = instanceDescriptor.familyName
        font.info.styleName = instanceDescriptor.styleName
        font.info.postscriptFontName = instanceDescriptor.postScriptFontName # yikes, note the differences in capitalisation..
        font.info.styleMapFamilyName = instanceDescriptor.styleMapFamilyName
        font.info.styleMapStyleName = instanceDescriptor.styleMapStyleName
                
        for sourceDescriptor in self.doc.sources:
            if sourceDescriptor.copyInfo:
                # this is the source
                if self.fonts[sourceDescriptor.name] is not None:
                    self._copyFontInfo(self.fonts[sourceDescriptor.name].info, font.info)
            if sourceDescriptor.copyLib:
                # excplicitly copy the font.lib items
                if self.fonts[sourceDescriptor.name] is not None:
                    for key, value in self.fonts[sourceDescriptor.name].lib.items():
                        font.lib[key] = value
            if sourceDescriptor.copyGroups:
                if self.fonts[sourceDescriptor.name] is not None:
                    for key, value in self.fonts[sourceDescriptor.name].groups.items():
                        font.groups[key] = value
            if sourceDescriptor.copyFeatures:
                if self.fonts[sourceDescriptor.name] is not None:
                    featuresText = self.fonts[sourceDescriptor.name].features.text
                    font.features.text = featuresText

        # ok maybe now it is time to calculate some glyphs
        # glyphs
        if glyphNames:
            selectedGlyphNames = glyphNames
        else:
            selectedGlyphNames = self.glyphNames
        if not 'public.glyphOrder' in font.lib.keys():
            # should be the glyphorder from the default, yes?
            font.lib['public.glyphOrder'] = selectedGlyphNames

        for glyphName in selectedGlyphNames:
            # can we take all this into a separate method for making a preview glyph object?
            glyphMutator, unicodes = self.getGlyphMutator(glyphName, discreteLocation=discreteLocation)
            if glyphMutator is None:
                if self.debug:
                    note = f"makeInstance: Could not make mutator for glyph {glyphName}"
                    self.logger.info(note)
                continue

            font.newGlyph(glyphName)
            font[glyphName].clear()
            glyphInstanceUnicodes = []
            #neutralFont = self.getNeutralFont()
            font[glyphName].unicodes = unicodes

            try:
                if not self.isAnisotropic(continuousLocation):
                    glyphInstanceObject = glyphMutator.makeInstance(continuousLocation, bend=bend)
                else:
                    # split anisotropic location into horizontal and vertical components
                    horizontalGlyphInstanceObject = glyphMutator.makeInstance(locHorizontal, bend=bend)
                    verticalGlyphInstanceObject = glyphMutator.makeInstance(locVertical, bend=bend)
                    # merge them again in a beautiful single line:
                    glyphInstanceObject = (1,0)*horizontalGlyphInstanceObject + (0,1)*verticalGlyphInstanceObject
            except IndexError:
                # alignment problem with the data?
                if self.debug:
                    note = "makeInstance: Quite possibly some sort of data alignment error in %s" % glyphName
                    self.logger.info(note)
                continue
            if self.roundGeometry:
                try:
                    glyphInstanceObject = glyphInstanceObject.round()
                except AttributeError:
                    # what are we catching here?
                    # math objects without a round method? 
                    if self.debug:
                        note = f"makeInstance: no round method for {glyphInstanceObject} ?"
                        self.logger.info(note)
            try:
                # File "/Users/erik/code/ufoProcessor/Lib/ufoProcessor/__init__.py", line 649, in makeInstance
                #   glyphInstanceObject.extractGlyph(font[glyphName], onlyGeometry=True)
                # File "/Applications/RoboFont.app/Contents/Resources/lib/python3.6/fontMath/mathGlyph.py", line 315, in extractGlyph
                #   glyph.anchors = [dict(anchor) for anchor in self.anchors]
                # File "/Applications/RoboFont.app/Contents/Resources/lib/python3.6/fontParts/base/base.py", line 103, in __set__
                #   raise FontPartsError("no setter for %r" % self.name)
                #   fontParts.base.errors.FontPartsError: no setter for 'anchors'
                if hasattr(font[glyphName], "fromMathGlyph"):
                    font[glyphName].fromMathGlyph(glyphInstanceObject)
                else:
                    glyphInstanceObject.extractGlyph(font[glyphName], onlyGeometry=True)
            except TypeError:
                # this causes ruled glyphs to end up in the wrong glyphname
                # but defcon2 objects don't support it
                pPen = font[glyphName].getPointPen()
                font[glyphName].clear()
                glyphInstanceObject.drawPoints(pPen)
            font[glyphName].width = glyphInstanceObject.width

        if self.debug:
            self.logger.info(f"\t\t\t{len(selectedGlyphNames)} glyphs added")
        return font

    def randomLocation(self, extrapolate=0):
        """A good random location, for quick testing and entertainment
        extrapolate is a scale of the min/max distance
        for discrete axes: random choice from the defined values
        for continuous axes: interpolated value between axis.minimum and axis.maximum
        """
        workLocation = {}
        for aD in self.getOrderedDiscreteAxes():
            workLocation[aD.name] = random.choice(aD.values)
        for aD in self.getOrderedContinuousAxes():
            if extrapolate:
                delta = (aD.maximum - aD.minimum)
                extraMinimum = aD.minimum - extrapolate*delta
                extraMaximum = aD.maximum + extrapolate*delta
            else:
                extraMinimum = aD.minimum
                extraMaximum = aD.maximum
            workLocation[aD.name] = ip(extraMinimum, extraMaximum, random.random())
        return workLocation

    @memoize
    def makeFontProportions(self, location, bend=False, roundGeometry=True):
        """Calculate the basic font proportions for this location, to map out expectations for drawing"""
        continuousLocation, discreteLocation = self.splitLocation(location)
        infoMutator = self.getInfoMutator(discreteLocation=discreteLocation)
        data = dict(unitsPerEm=1000, ascender=750, descender=-250, xHeight=500)
        if infoMutator is None:
            return data
        if not self.isAnisotropic(location):
            infoInstanceObject = infoMutator.makeInstance(loc, bend=bend)
        else:
            horizontalInfoInstanceObject = infoMutator.makeInstance(locHorizontal, bend=bend)
            verticalInfoInstanceObject = infoMutator.makeInstance(locVertical, bend=bend)
            # merge them again
            infoInstanceObject = (1,0)*horizontalInfoInstanceObject + (0,1)*verticalInfoInstanceObject
        if roundGeometry:
            infoInstanceObject = infoInstanceObject.round()
        data = dict(unitsPerEm=infoInstanceObject.unitsPerEm, ascender=infoInstanceObject.ascender, descender=infoInstanceObject.descender, xHeight=infoInstanceObject.xHeight)
        print(dir(infoInstanceObject))
        return data

    # cache? could cause a lot of material in memory that we don't really need. Test this!
    @memoize
    def makeOneGlyph(self, glyphName, location, bend=False, decomposeComponents=True, useVarlib=False, roundGeometry=False, clip=False):
        """
        glyphName: 
        location: location including discrete axes
        bend: apply axis transformations
        decomposeComponents: decompose all components so we get a proper representation of the shape
        useVarlib: use varlib as mathmodel. Otherwise it is mutatorMath
        roundGeometry: round all geometry to integers
        clip: restrict axis values to the defined minimum and maximum

        + Supports extrapolation for varlib and mutatormath: though the results can be different
        + Supports anisotropic locations for varlib and mutatormath. Obviously this will not be present in any Variable font exports.

        Returns: a mathglyph, results are cached
        """
        continuousLocation, discreteLocation = self.splitLocation(location)
        # check if the discreteLocation is within limits
        if not self.checkDiscreteAxisValues(discreteLocation):
            if self.debug:
                self.logger.info(f"\t\tmakeOneGlyph reports: {location} has illegal value for discrete location")
            return None
        previousModel = self.useVarlib
        self.useVarlib = useVarlib
        glyphMutator, unicodes = self.getGlyphMutator(glyphName, decomposeComponents=decomposeComponents, discreteLocation=discreteLocation)
        if not glyphMutator: return None
        try:
            if not self.isAnisotropic(location):
                glyphInstanceObject = glyphMutator.makeInstance(continuousLocation, bend=bend)
            else:
                anisotropic = True
                if self.debug:
                    self.logger.info(f"\t\tmakeOneGlyph anisotropic location: {location}")
                loc = Location(continuousLocation)
                locHorizontal, locVertical = self.splitAnisotropic(loc)
                # split anisotropic location into horizontal and vertical components
                horizontalGlyphInstanceObject = glyphMutator.makeInstance(locHorizontal, bend=bend)
                verticalGlyphInstanceObject = glyphMutator.makeInstance(locVertical, bend=bend)
                # merge them again
                glyphInstanceObject = (1,0)*horizontalGlyphInstanceObject + (0,1)*verticalGlyphInstanceObject
                if self.debug:
                    self.logger.info(f"makeOneGlyph anisotropic glyphInstanceObject {glyphInstanceObject}")
        except IndexError:
            # alignment problem with the data?
            if self.debug:
                note = "makeOneGlyph: Quite possibly some sort of data alignment error in %s" % glyphName
                self.logger.info(note)
                return None
        glyphInstanceObject.unicodes = unicodes
        if roundGeometry:
            glyphInstanceObject.round()
        self.useVarlib = previousModel
        return glyphInstanceObject

    def _copyFontInfo(self, sourceInfo, targetInfo):
        """ Copy the non-calculating fields from the source info."""
        infoAttributes = [
            "versionMajor",
            "versionMinor",
            "copyright",
            "trademark",
            "note",
            "openTypeGaspRangeRecords",
            "openTypeHeadCreated",
            "openTypeHeadFlags",
            "openTypeNameDesigner",
            "openTypeNameDesignerURL",
            "openTypeNameManufacturer",
            "openTypeNameManufacturerURL",
            "openTypeNameLicense",
            "openTypeNameLicenseURL",
            "openTypeNameVersion",
            "openTypeNameUniqueID",
            "openTypeNameDescription",
            "#openTypeNamePreferredFamilyName",
            "#openTypeNamePreferredSubfamilyName",
            "#openTypeNameCompatibleFullName",
            "openTypeNameSampleText",
            "openTypeNameWWSFamilyName",
            "openTypeNameWWSSubfamilyName",
            "openTypeNameRecords",
            "openTypeOS2Selection",
            "openTypeOS2VendorID",
            "openTypeOS2Panose",
            "openTypeOS2FamilyClass",
            "openTypeOS2UnicodeRanges",
            "openTypeOS2CodePageRanges",
            "openTypeOS2Type",
            "postscriptIsFixedPitch",
            "postscriptForceBold",
            "postscriptDefaultCharacter",
            "postscriptWindowsCharacterSet"
        ]
        for infoAttribute in infoAttributes:
            copy = False
            if self.ufoVersion == 1 and infoAttribute in fontInfoAttributesVersion1:
                copy = True
            elif self.ufoVersion == 2 and infoAttribute in fontInfoAttributesVersion2:
                copy = True
            elif self.ufoVersion == 3 and infoAttribute in fontInfoAttributesVersion3:
                copy = True
            if copy:
                value = getattr(sourceInfo, infoAttribute)
                setattr(targetInfo, infoAttribute, value)



if __name__ == "__main__":
    import time, random
    from fontParts.world import RFont
    ds5Path = "../../Tests/ds5/ds5.designspace"
    dumpCacheLog = True
    makeUFOs = True
    import os
    if os.path.exists(ds5Path):
        startTime = time.time()
        doc = UFOOperator(ds5Path, useVarlib=True, debug=True)
        doc.loadFonts()
        print("collectForegroundLayerNames", doc.collectForegroundLayerNames())
        if makeUFOs:
            doc.generateUFOs()
        #doc.updateFonts([f])
        #doc._logLoadedFonts()
        loc = doc.newDefaultLocation()
        res = doc.makeOneGlyph("glyphOne", location=loc)
        
        if dumpCacheLog:
            doc.logger.info(f"Test: cached {len(_memoizeCache)} items")
            for key, item in _memoizeCache.items():
                doc.logger.info(f"\t\t{key} {item}")
        endTime = time.time()
        duration = endTime - startTime
        print(f"duration: {duration}" )

        inspectMemoizeCache()
        for key, value in _memoizeStats.items():
            print(key[0], value)

        # make some font proportions
        props = doc.makeFontProportions(loc)
        print(props)

        for i in range(10):
            print(doc.randomLocation(extrapolate=0.1))