#
# Copyright (c) SAS Institute Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


import itertools
import os
import re
import stat

from conary.deps import deps
from conary.lib import util, magic
from conary.local import database
from conary.build import policy
from conary.build import use


def _providesNames(libname):
    provideList = [libname]
    if libname.endswith(':lib') or libname.endswith(':devellib'):
        # Instead of requiring the :lib or :devellib component that satisfies
        # the dependency, our first choice, if possible, is to
        # require :devel, because that would include header files;
        # if it does not exist, then :devellib for a soname link;
        # finally if neither of those exists, then :lib (though
        # that is a degenerate case).
        pkg=libname.split(':')[0]
        provideList = [
            pkg+':devel',
            pkg+':devellib',
            pkg+':lib',
        ]
    return provideList


def _reduceCandidates(db, foundCandidates):
    # this may not be the most efficient algorithm, but almost
    # every case will be two providers (:devel and :devellib)
    # so it doesn't matter

    def satisfies(a, b):
        return db.getTrove(*a).getProvides().intersection(
                   db.getTrove(*b).getRequires())

    if len(foundCandidates) < 2:
        return foundCandidates

    a = foundCandidates[0]
    b = foundCandidates[1]
    c = foundCandidates[2:]

    if satisfies(a, b):
        return _reduceCandidates(db, [a] + c)
    if satisfies(b, a):
        return _reduceCandidates(db, [b] + c)

    if c:
        return sorted(list(set(
            _reduceCandidates(db, [a] + c) +
            _reduceCandidates(db, [b] + c))))

    return [a, b]

def reportFoundBuildRequires(recipe, reqList):
    # Report FOUND build requirements to the 
    # reportExcessBuildRequires policy so that it
    # can determine which of the requirements were
    # not found
    try:
        recipe.reportExcessBuildRequires(reqList)
    except AttributeError:
        # it is OK if we are running with an earlier Conary that
        # does not have reportExcessBuildRequires
        pass

def reportMissingBuildRequires(recipe, reqList):
    try:
        recipe.reportMissingBuildRequires(reqList)
    except AttributeError:
        # it is OK if we are running with an earlier Conary that
        # does not have reportMissingBuildRequires
        pass


class _warnBuildRequirements(policy.EnforcementPolicy):
    def setTalk(self):
        # FIXME: remove "True or " when we are ready for errors
        if (True or 'local@local' in self.recipe.macros.buildlabel
            or use.Use.bootstrap._get()):
            self.talk = self.warn
        else:
            self.talk = self.error

    def _initComponentExceptions(self):
        self.compExceptions = set()
        self.compReExceptions = set()
        compRe = re.compile('[a-zA-Z0-9]+:[a-zA-Z0-9]+')
        if self.exceptions:
            for exception in self.exceptions:
                exception = exception % self.recipe.macros
                if compRe.match(exception):
                    self.compExceptions.add(exception)
                else:
                    self.compReExceptions.add(re.compile(exception))
        self.exceptions = None

    def _removeExceptions(self, candidates):
        candidates = candidates - self.compExceptions
        for compRe in self.compReExceptions:
            candidates -= set(x for x in candidates if compRe.match(x))
        return candidates

    def _removeExceptionsFromList(self, candidates):
        candidateList = []
        for item in candidates:
            if item not in self.compExceptions and not [compRe
                for compRe in self.compReExceptions
                if compRe.match(item)]:
                candidateList.append(item)
        return candidateList


class _enforceBuildRequirements(_warnBuildRequirements):
    """
    Pure virtual base class from which classes are derived that
    enforce buildRequires population from runtime dependencies.
    """
    processUnmodified = False
    ignoreCapsuleFiles = False

    def test(self):
        components = self.recipe.autopkg.components
        reqDepSet = deps.DependencySet()
        provDepSet = deps.DependencySet()
        for pkg in components.values():
            reqDepSet.union(pkg.requires)
            provDepSet.union(pkg.provides)
        self.depSet = deps.DependencySet()
        self.depSet.union(reqDepSet - provDepSet)

        depSetList = [ ]
        for dep in self.depSet.iterDepsByClass(self.depClass):
            depSet = deps.DependencySet()
            depSet.addDep(self.depClass, dep)
            depSetList.append(depSet)

        if not depSetList:
            return False

        self._initComponentExceptions()

        cfg = self.recipe.cfg
        self.db = database.Database(cfg.root, cfg.dbPath)
        self.systemProvides = self.db.getTrovesWithProvides(depSetList)
        self.unprovided = [x for x in depSetList if x not in self.systemProvides]

        self.transitiveBuildRequires = self.recipe._getTransitiveBuildRequiresNames()
        # For compatibility with older external policy that derives from this
        self.truncatedBuildRequires = self.transitiveBuildRequires

        self.setTalk()
        self.missingBuildRequires = set()

        return True

    def addMissingBuildRequires(self, missingList):
        self.missingBuildRequires.update(missingList)

    def reportMissingBuildRequires(self):
        self.talk('add to buildRequires: %s',
                   str(sorted(list(set(self.missingBuildRequires)))))
        reportMissingBuildRequires(self.recipe, self.missingBuildRequires)

    def postProcess(self):
        del self.db

    def do(self):
        missingBuildRequiresChoices = []

        components = self.recipe.autopkg.components
        pathMap = self.recipe.autopkg.pathMap
        pathReqMap = {}
        interpreterSet = set()

        interpreterMap = {}
        for path in pathMap:
            if (hasattr(self.recipe, '_isDerived')
                and self.recipe._isDerived == True
                and self.processUnmodified is False
                and path in self.recipe._derivedFiles
                and not self.mtimeChanged(path)):
                # ignore this file
                continue
            if self.ignoreCapsuleFiles and hasattr(self.recipe,
                                              '_getCapsulePathsForFile'):
                if self.recipe._getCapsulePathsForFile(path):
                    continue
            pkgfile = pathMap[path]
            if pkgfile.hasContents:
                m = self.recipe.magic[path]
                if isinstance(m, magic.script):
                    interpreter = m.contents['interpreter']
                    if interpreter:
                        interpreterMap[path] = (pkgfile.requires(), interpreter)

        provideNameMap = dict([(x[0], x) for x in
                               itertools.chain(*self.systemProvides.values())])

        for dep in self.systemProvides:
            provideNameList = [x[0] for x in self.systemProvides[dep]]
            # normally, there is only one name in provideNameList

            foundCandidates = set()
            for name in provideNameList:
                for candidate in _providesNames(name):
                    if self.db.hasTroveByName(candidate):
                        foundCandidates.add(candidate)
                        provideNameMap[candidate] = provideNameMap[name]
                        break
            # report before exceptions
            reportFoundBuildRequires(self.recipe, foundCandidates)
            foundCandidates = self._removeExceptions(foundCandidates)

            missingCandidates = foundCandidates - self.transitiveBuildRequires
            if foundCandidates and missingCandidates == foundCandidates:
                # None of the troves that provides this requirement is
                # reflected in the buildRequires list.  Add candidates
                # to proper list to print at the end:
                if len(foundCandidates) > 1:
                    reduceTroves = sorted([provideNameMap[x]
                                          for x in foundCandidates])
                    reduceTroves = _reduceCandidates(self.db, reduceTroves)
                    foundCandidates = set([x[0] for x in reduceTroves])
                    if len(foundCandidates) == 1:
                        break
                    found = False
                    for candidateSet in missingBuildRequiresChoices:
                        if candidateSet == foundCandidates:
                            found = True
                    if found == False:
                        self.addMissingBuildRequires(foundCandidates)
                else:
                    self.addMissingBuildRequires(foundCandidates)

                # Now give lots of specific information to help the packager
                # in case things do not look so obvious...
                pathList = []
                for path in pathMap:
                    if (hasattr(self.recipe, '_isDerived')
                        and self.recipe._isDerived == True
                        and self.processUnmodified is False
                        and path in self.recipe._derivedFiles
                        and not self.mtimeChanged(path)):
                        # ignore this file
                        continue
                    if self.ignoreCapsuleFiles and hasattr(self.recipe,
                                                  '_getCapsulePathsForFile'):
                        if self.recipe._getCapsulePathsForFile(path):
                            continue
                    pkgfile = pathMap[path]
                    if pkgfile.hasContents and (pkgfile.requires() & dep):
                        pathList.append(path)
                        l = pathReqMap.setdefault(path, [])
                        l.append(dep)
                if pathList:
                    self.warn('buildRequires %s needed to satisfy "%s"'
                              ' for files: %s',
                              str(sorted(list(foundCandidates))),
                              str(dep),
                              ', '.join(sorted(pathList)))

            # look for interpreters
            if path in interpreterMap:
                requires, interpreter = interpreterMap[path]
                if requires & dep:
                    interpreterSet.add(interpreter)

        if interpreterSet:
            # find their components and add them to the list
            for interpreter in interpreterSet:
                for trove in self.db.iterTrovesByPath(interpreter):
                    interpreterTroveName = trove.getName()
                    if interpreterTroveName not in self.transitiveBuildRequires:
                        self.talk('interpreter %s missing build requirement %s',
                                  interpreter, interpreterTroveName)
                        self.addMissingBuildRequires((interpreterTroveName,))

        if pathReqMap:
            for path in pathReqMap:
                self.warn('file %s has unsatisfied build requirements "%s"',
                          path, '", "'.join([
                             str(x) for x in
                               sorted(list(set(pathReqMap[path])))]))

        if self.missingBuildRequires:
            self.reportMissingBuildRequires()

        if missingBuildRequiresChoices:
            for candidateSet in missingBuildRequiresChoices:
                self.talk('add to buildRequires one of: %s',
                           str(sorted(list(candidateSet))))
            # These are too unclear to pass to reportMissingBuildRequires
        if self.unprovided:
            self.talk('The following dependencies are not resolved'
                      ' within the package or in the system database: %s',
                      str(sorted([str(x) for x in self.unprovided])))
            self.talk('The package may not function if installed, and'
                      ' Conary may require the --no-deps option to install the'
                      ' package.')
            self.talk('If you know that these libraries are really provided'
                      ' within the package, add these lines:')
            for depStr in sorted(str(x) for x in self.unprovided):
                self.talk("       r.Requires(exceptDeps=r'%s')" % 
                           util.literalRegex(depStr))


class EnforceSonameBuildRequirements(_enforceBuildRequirements):
    """
    NAME
    ====

    B{C{r.EnforceSonameBuildRequirements()}} - Ensure package requires
    shared library dependencies match elements in r.buildRequires list

    SYNOPSIS
    ========

    C{r.EnforceSonameBuildRequirements([I{filterexp}] || [I{exceptions='I{pkg}:I{comp}'}])}

    DESCRIPTION
    ===========

    The C{r.EnforceSonameBuildRequirements()} policy ensures that each
    requires dependency in the package is matched by a suitable element in
    the C{buildRequires} list.

    Any trove names wrongly suggested can be eliminated from the
    list with C{r.EnforceSonameBuildRequirements(exceptions='I{pkg}:I{comp}')}.

    EXAMPLES
    ========

    C{r.EnforceSonameBuildRequirements(exceptions='.*')}

    Useful when packaging pre-built executables which do not need to
    (and cannot) be linked at cook time to development libraries
    specified in C{buildRequires}.
    """

    depClassType = deps.DEP_CLASS_SONAME
    depClass = deps.SonameDependencies
    ignoreCapsuleFiles = True

    def reportMissingBuildRequires(self):
        _enforceBuildRequirements.reportMissingBuildRequires(self)
        self.recipe.EnforceStaticLibBuildRequirements(
            warnedSoNames=self.missingBuildRequires)


class EnforcePythonBuildRequirements(_enforceBuildRequirements):
    """
    NAME
    ====

    B{C{r.EnforcePythonBuildRequirements()}} - Ensure package meets Python
    runtime requirements

    SYNOPSIS
    ========

    C{r.EnforcePythonBuildRequirements([I{filterexp}] || [I{exceptions='I{pkg}:I{comp}'}])}

    DESCRIPTION
    ===========

    The C{r.EnforcePythonBuildRequirements()} policy ensures that Python
    runtime requirements are met by the package, or by components listed in
    the C{buildRequires} list.  In general, missing Python build
    requirements will translate into missing or incomplete Python
    runtime requirements.

    Any trove names wrongly suggested should be eliminated from consideration
    by using C{r.Requires(exceptDeps='python: ...')}

    Note that python requirements cannot be calculated unless the
    providing packages are actually installed on the system.  For
    this reason, exceptions to the C{r.EnforcePythonBuildRequirements()}
    policy are strongly discouraged.
    """

    depClassType = deps.DEP_CLASS_PYTHON
    depClass = deps.PythonDependencies
    ignoreCapsuleFiles = False


class EnforceJavaBuildRequirements(_enforceBuildRequirements):
    """
    NAME
    ====

    B{C{r.EnforceJavaBuildRequirements()}} - Ensure package meets Java
    runtime requirements

    SYNOPSIS
    ========

    C{r.EnforceJavaBuildRequirements([I{filterexp}] || [I{exceptions='I{pkg}:I{comp}'}])}

    DESCRIPTION
    ===========

    The C{r.EnforceJavaBuildRequirements()} policy ensures that Java runtime
    requirements are met by the package, or by components listed in the
    C{buildRequires} list.

    Any trove names wrongly suggested can be eliminated from the
    list with C{r.EnforceJavaBuildRequirements(exceptions='I{pkg}:I{comp}')}.

    EXAMPLES
    ========

    C{r.EnforceJavaBuildRequirements(exceptions='.*')}

    This turns off all enforcement of Java build requirements, which is
    particularly useful when packaging pre-built Java applications
    that are not executed on the system on which they are installed,
    but are instead provided to other systems (likely via HTTP to a remote
    web browser), then you should instead remove the runtime requirements
    entirely with C{r.Requires(exceptions='.*\.(java|jar|zip)')} (the
    fastest approach) or C{r.Requires(exceptDeps='java:.*')} (slower
    but more accurate).

    Note that Java requirements satisfied neither on the system
    nor within the package are automatically eliminated from
    package requirements, so you should provide exceptions only
    for components that are likely to be installed on the system
    at build time but not actually required at run time.
"""

    depClassType = deps.DEP_CLASS_JAVA
    depClass = deps.JavaDependencies
    ignoreCapsuleFiles = True

    # FIXME: remove this when we are ready to enforce Java dependencies
    def setTalk(self):
        self.talk = self.warn


class EnforceCILBuildRequirements(_enforceBuildRequirements):
    """
    NAME
    ====

    B{C{r.EnforceCILBuildRequirements()}} - Ensure package meets CIL
    runtime requirements

    SYNOPSIS
    ========

    C{r.EnforceCILBuildRequirements([I{filterexp}] || [I{exceptions='I{pkg}:I{comp}'}])}

    DESCRIPTION
    ===========

    The C{r.EnforceCILBuildRequirements()} policy ensures that CIL runtime
    requirements are met by the package, or by components listed in the
    C{buildRequires} list.

    Any trove names wrongly suggested can be eliminated from the
    list with C{r.EnforceCILBuildRequirements(exceptions='I{pkg}:I{comp}')}.

    EXAMPLES
    ========

    C{r.EnforceCILBuildRequirements(exceptions='.*')}

    Useful when packaging pre-built CIL files.
    """

    depClassType = deps.DEP_CLASS_CIL
    depClass = deps.CILDependencies
    ignoreCapsuleFiles = True

    def test(self):
        CILDeps = _enforceBuildRequirements.test(self)
        if CILDeps:
            if 'mono:devel' not in self.transitiveBuildRequires:
                self.addMissingBuildRequires(('mono:devel',))
        return CILDeps


class EnforcePerlBuildRequirements(_enforceBuildRequirements):
    """
    NAME
    ====

    B{C{r.EnforcePerlBuildRequirements()}} - Ensure package meets Perl
    runtime requirements

    SYNOPSIS
    ========

    C{r.EnforcePerlBuildRequirements([I{filterexp}] || [I{exceptions='I{pkg}:I{comp}'}])}

    DESCRIPTION
    ===========

    The C{r.EnforcePerlBuildRequirements()} policy ensures that Perl runtime
    requirements are met by the package, or by components listed in the
    C{buildRequires} list.  In general, missing Perl build
    requirements will translate into missing or incomplete Perl
    runtime requirements.

    Any trove names wrongly suggested should be eliminated from consideration
    by using C{r.Requires(exceptDeps='perl: ...')}
    """

    depClassType = deps.DEP_CLASS_PERL
    depClass = deps.PerlDependencies
    ignoreCapsuleFiles = False


class _enforceLogRequirements(policy.EnforcementPolicy):
    """
    Abstract base class
    """

    filetree = policy.BUILDDIR
    invariantinclusions = []

    # list of regular expressions (using macros) that cause an
    # entry to be ignored unless a related strings is found in
    # another named file (empty tuple is unconditional blacklist)
    greylist = []
    # list of (handler, startRe, stopRe) tuples defining sets
    # of lines that should be provided to a handler to do
    # something about.  If stopRe is None, handler takes one line
    stanzaList = []

    # Regexp to search dependencies
    foundRe = ''

    def test(self):
        if self.recipe.ignoreDeps:
            return False

        self.foundPaths = set()
        self.greydict = {}
        # interpolate macros, compile regexps
        macros = self.macros
        for greyPath, greyTup in self.greylist:
            self.greydict[greyPath % macros] = tuple(
                (x, re.compile(y % macros)) for x, y in greyTup)
        stanzaList = []
        for handler, startRe, stopRe in self.stanzaList:
            stanzaList.append((handler,
                               re.compile(startRe%macros),
                               re.compile(stopRe%macros)))
        self.stanzaList = stanzaList

        # process exceptions differently; user can specify either the
        # source (found path) or destination (found component) to ignore
        self.pathExceptions = set()
        self.compExceptions = set()
        if self.exceptions:
            for exception in self.exceptions:
                exception = exception % self.recipe.macros
                if '/' in exception:
                    self.pathExceptions.add(exception)
                else:
                    self.compExceptions.add(exception)
        # never suggest a recursive buildRequires
        self.compExceptions.update(set(self.recipe.autopkg.components.keys()))
        self.exceptions = None

        return True

    def greylistFilter(self, foundPaths, fullpath):
        pass

    def foundPath(self, line):
        match = self.foundRe.match(line)
        if match:
            return match.group(1)
        return False


    def doFile(self, path):

        fullpath = self.macros.builddir + path

        # A stanza is any portion of a config file that can be
        # recognized by regular expressions for start and optionally
        # end lines.  Multiple stanzas can overlap (improper nesting is
        # allowed), but only one stanza of any type can be collected
        # at once (any particular stanza type does not nest) to avoid
        # collecting long, useless lists due to insufficient specification
        # in the regular expressions.
        # Also, there are regular expressions (foundRe) just for
        # quickly finding individual paths in individual lines
        # lines to be emitted that aren't part of stanzas but
        # just represent paths that are known to have to exist;
        # it's just faster to process the file once instead of twice.

        def foundStanzaStart(line):
            for handler, startRe, stopRe in self.stanzaList:
                match = startRe.match(line)
                if match:
                    yield handler, stopRe, match.groups(), 

        def iterConfigStanzas(lines, fullpath):
            openStanzas = {}
            openStanzaLines = {}
            for line in (x.rstrip('\n') for x in lines):

                # the trivial case of the known-needed path on one line
                foundPath = self.foundPath(line)
                if foundPath:
                    yield foundPath

                # do this before checking for startStanzas so that if
                # the start and stop regexp are the same, you return
                # the lines of the file segmented by that regexp
                for handler in openStanzas.keys(): # not iterkeys()
                    openStanzaLines[handler].append(line)
                    stopRe, startGroups = openStanzas[handler]
                    match = stopRe.match(line)
                    if match:
                        openStanzas.pop(handler)
                        handler(startGroups, match.groups(),
                                openStanzaLines[handler], fullpath)

                for handler, stopRe, startGroups in foundStanzaStart(line):
                    if handler in openStanzas:
                        # report partial stanza
                        handler(startGroups, None, openStanzaLines[handler],
                                fullpath)
                    if stopRe is None:
                        handler(startGroups, line, fullpath)
                    else:
                        openStanzas[handler] = (stopRe, startGroups)
                        openStanzaLines[handler] = [line]

            # handle any open stanzas after reading the file
            for handler in openStanzas.iterkeys():
                stopRe, startGroups = openStanzas[handler]
                handler(startGroups, None, openStanzaLines[handler], fullpath)


        # iterator to avoid reading in the whole file at once;
        # nested iterators to avoid matching regexp twice
        foundPaths = set(path for path in iterConfigStanzas(
                            file(fullpath), fullpath)
                         if path not in self.pathExceptions)

        # now remove false positives using the greylist
        if self.greydict:
            foundPaths = set(self.greylistFilter(foundPaths, fullpath))

        self.foundPaths.update(foundPaths)

    def postProcess(self):
        if not self.foundPaths:
            return

        db = database.Database(self.recipe.cfg.root, self.recipe.cfg.dbPath)

        # first, get all the trove names in the transitive buildRequires
        # runtime dependency closure
        transitiveBuildRequires = self.recipe._getTransitiveBuildRequiresNames()

        # next, for each file found, report if it is not in the
        # transitive closure of runtime requirements of buildRequires
        fileReqs = set()
        for path in sorted(self.foundPaths):
            for pathReq in set(trove.getName()
                               for trove in db.iterTrovesByPath(path)):
                pathReqCandidates = _providesNames(pathReq)
                # remove any recursive or non-existing buildreqs
                pathReqCandidates = [x for x in pathReqCandidates 
                                     if db.hasTroveByName(x) and
                                        x not in self.compExceptions]
                # do not warn about any of these candidates being excessive
                reportFoundBuildRequires(self.recipe, pathReqCandidates)
                # display only the best choice
                thisFileReq = set(pathReqCandidates[0:1])
                missingReqs = thisFileReq - transitiveBuildRequires
                if missingReqs:
                    self.warn('path %s suggests buildRequires: %s',
                              path, ', '.join((sorted(list(missingReqs)))))
                fileReqs.update(thisFileReq)

        # finally, give the coalesced suggestion for cut and paste
        # into the recipe if all the individual messages make sense
        missingReqs = fileReqs - transitiveBuildRequires
        if missingReqs:
            self.warn('Probably add to buildRequires: %s',
                      str(sorted(list(missingReqs))))
            reportMissingBuildRequires(self.recipe, missingReqs)


class EnforceConfigLogBuildRequirements(_enforceLogRequirements):
    """
    NAME
    ====

    B{C{r.EnforceConfigLogBuildRequirements()}} - Ensures that components
    mentioned in config.log files are listed as build requirements

    SYNOPSIS
    ========

    C{r.EnforceConfigLogBuildRequirements([I{filterexp}] || [I{/path/to/file/found}] || [I{exceptions='I{pkg}:I{comp}'}])}

    DESCRIPTION
    ===========

    The C{r.EnforceConfigLogBuildRequirements()} policy ensures that components
    containing files mentioned in C{config.log} files are listed as build
    requirements.

    EXAMPLES
    ========

    C{r.EnforceConfigLogBuildRequirements(exceptions='flex:runtime')}

    This disables a requirement for flex:runtime; this would normally
    be because the C{configure} program checked for flex, but does not
    actually need to run it because the program is shipped with a
    prebuilt lexical analyzer.
    """

    filetree = policy.BUILDDIR
    invariantinclusions = [ (r'.*/config\.log', 0400, stat.S_IFDIR), ]

    greylist = [
        # config.log string, ((filename, regexp), ...)
        ('%(prefix)s/X11R6/bin/makedepend', ()),
        ('%(bindir)s/g77',
            (('configure.ac', r'\s*AC_PROG_F77'),
             ('configure.in', r'\s*AC_PROG_F77'))),
        ('%(bindir)s/gfortran',
            (('configure.ac', r'\s*AC_PROG_F77'),
             ('configure.in', r'\s*AC_PROG_F77'))),
        ('%(bindir)s/bison',
            (('configure.ac', r'\s*AC_PROC_YACC'),
             ('configure.in', r'\s*(AC_PROG_YACC|YACC=)'))),
    ]

    foundRe = re.compile('^[^ ]+: found (/([^ ]+)?bin/[^ ]+)$')
    # we do not find sys/wait.h in
    # "sys/wait.h that is POSIX.1 compatible"
    # because we do not want to be confused by
    # "checking whether time.h and sys/time.h may both be included"
    headerRe = re.compile(r'^[^/].*\.h$')
    # "checking for foo in -lbar" success implies found -lbar
    libRe = re.compile(r'.* -l([-a-zA-Z_]*)$')

    def __init__(self, *args, **kw):
        self.stanzaList = [
            (self.handleCheck, 'configure:[0-9]+: checking for  *(.*)',
                               'configure:[0-9]+: result:  *(.*)')
        ]
        _enforceLogRequirements.__init__(self, *args, **kw)

    def parseSuccess(self, token):
        if not token:
            # empty string, such as looking for executable suffix
            return False
        if token == 'yes':
            return True
        if token.split()[0] in set(('no', 'not', 'done', 'failed',
                                    'none', 'disabled',)):
            return False
        return token

    def handleCheck(self, startGroups, stopGroups, lines, fullpath):

        if stopGroups is None:
            # we lost sync, don't start guessing because we care about
            # the result of the check
            return
        sought = startGroups[0]
        success = self.parseSuccess(stopGroups[0])
        includeDirs = [ '%(includedir)s/' %self.macros]
        root = self.recipe.cfg.root

        if success:
            if self.headerRe.match(sought):
                # look for header files
                for tokens in (x.split() for x in lines):
                    for token in tokens:
                        if token.startswith('-I/') and len(token) > 3:
                            includeDirs.append(token[2:])
                for dirName in includeDirs:
                    seekPath = util.normpath('%s/%s' %(dirName, sought))
                    if util.exists('%s%s' %(root, seekPath)):
                        self.foundPaths.add(seekPath)
                        break

            libName = self.libRe.match(sought)
            if libName:
                libName = libName.group(0)
                # Take advantage of the fact that the actual test will
                # include running the compiler with the library in the
                # link line in such a way that the
                # EnforceStaticLibBuildRequirements policy knows how
                # to understand it.
                # EnforceStaticLibBuildRequirements does not handle the
                # leading "configure:01234: " portion of the output,
                # so give it every line that has further content and
                # let it find the lines that it cares about
                logLines = (x.split(': ', 1) for x in lines)
                logLines = (x[1] for x in logLines if len(x) > 1)
                self.recipe.EnforceStaticLibBuildRequirements(logLines=logLines)

            candidate = None
            if sought.startswith('/'):
                candidate = sought
            elif type(success) is str and success.startswith('/'):
                candidate = success
            if candidate:
                # configure:2345: checking for /bin/sought
                # configure:6543: result: yes
                # configure:4441: checking for egrep
                # configure:4519: result: /bin/grep -E
                # configure:4535: checking for ld used by gcc
                # configure:4602: result: /usr/bin/ld
                seekPath = candidate.split()[0]
                if util.exists(util.normpath('%s%s' %(root, seekPath))):
                    self.foundPaths.update(set(
                        self.greylistFilter(set((seekPath,)), fullpath)))

            # Anything we do not specifically recognize is ignored
        # all failed cases are ignored


    def greylistFilter(self, foundPaths, fullpath):
        # remove false positives using the greylist
        for foundPath in foundPaths:
            if foundPath not in self.greydict:
                # do not even consider removing from the set
                yield foundPath
            else:
                foundMatch = False
                for otherFile, testRe in self.greydict[foundPath]:
                    otherFile = fullpath.replace('config.log', otherFile)
                    if not foundMatch and os.path.exists(otherFile):
                        otherFile = file(otherFile)
                        if [line for line in otherFile if testRe.match(line)]:
                            foundMatch = True
                if foundMatch:
                    # greylist has found a confirming entry in another
                    # file that indicates that this entry is real
                    yield foundPath


class EnforceCMakeCacheBuildRequirements(_enforceLogRequirements):
    """
    NAME
    ====

    B{C{r.EnforceCMakeCacheBuildRequirements()}} - Ensures that components
    mentioned in CMakeCache.txt files are listed as build requirements

    SYNOPSIS
    ========

    C{r.EnforceCMakeCacheBuildRequirements([I{filterexp}] || [I{/path/to/file/found}] || [I{exceptions='I{pkg}:I{comp}'}])}

    DESCRIPTION
    ===========

    The C{r.EnforceCMakeCacheBuildRequirements()} policy ensures that components
    containing files mentioned in C{CMakeCache.txt} files are listed as build
    requirements.

    EXAMPLES
    ========

    C{r.EnforceCMakeCacheBuildRequirements(exceptions='flex:runtime')}

    This disables a requirement for flex:runtime; this would normally
    be because the C{cmake} program checked for flex, but does not
    actually need to run it because the program is shipped with a
    prebuilt lexical analyzer.
    """

    filetree = policy.BUILDDIR
    invariantinclusions = [ (r'.*/CMakeCache\.txt', 0400, stat.S_IFDIR), ]

    foundRe = re.compile('^[^ ]+:FILEPATH=(/[^ ]+)$')


class EnforceFlagBuildRequirements(_warnBuildRequirements):
    """
    NAME
    ====

    B{C{r.EnforceFlagBuildRequirements()}} - Ensures that all files
    used to define the current flavor are listed as build requirements

    B{C{EnforceFlagBuildRequirements}} should never be called, and
    takes no exceptions.

    """
    processUnmodified = True
    def test(self):
        # use flags track their provider only if the
        # _getTransitiveBuildRequiresNames recipe method exists
        try:
            self.transitiveBuildRequires = self.recipe._getTransitiveBuildRequiresNames()
        except AttributeError:
            return False

        cfg = self.recipe.cfg
        self.db = database.Database(cfg.root, cfg.dbPath)
        self.setTalk()

        return True

    def postProcess(self):
        del self.db

    def do(self):
        missingBuildRequires = set()
        foundBuildRequires = set()
        for flag in use.iterUsed():
            if (hasattr(self.recipe, '_isDerived')
                and self.recipe._isDerived == True):
                # In a derived recipe, enforce this only for added flags
                if flag is use.UseFlag:
                    for dep in self.recipe.useFlags.iterDeps():
                        if dep[0] is deps.UseDependency:
                            if flag.name in dep[1].flags:
                                continue
            path = flag._path
            for trove in self.db.iterTrovesByPath(path):
                flagTroveName = trove.getName()
                if flagTroveName in self.transitiveBuildRequires:
                    foundBuildRequires.add(flagTroveName)
                else:
                    self.talk('flag %s missing build requirement %s',
                              flag._name, flagTroveName)
                    missingBuildRequires.add(flagTroveName)

        if missingBuildRequires:
            self.talk('add to buildRequires: %s',
                       str(sorted(list(set(missingBuildRequires)))))
            reportMissingBuildRequires(self.recipe, missingBuildRequires)

        if foundBuildRequires:
            reportFoundBuildRequires(self.recipe, foundBuildRequires)


class EnforceStaticLibBuildRequirements(_warnBuildRequirements):
    """
    NAME
    ====

    B{C{r.EnforceStaticLibBuildRequirements()}} - Ensures that components
    which provide static libraries mentioned in compile or link commands
    are listed as build requirements.

    SYNOPSIS
    ========

    C{r.EnforceStaticLibBuildRequirements([I{exceptions='I{pkg}:I{comp}'}])}

    DESCRIPTION
    ===========

    The C{r.EnforceStaticLibBuildRequirements()} policy looks at
    output from the build process for lines that start with a compiler
    or linker invocation and include a C{-llibname} command-line
    argument where no shared library build requirement has been
    found via C{soname:} requirements.

    EXAMPLES
    ========

    C{r.EnforceStaticLibBuildRequirements(exceptions='acl:devel')

    This disables a requirement for acl:devel; this would normally
    be because a line of output included "-lacl" without the package
    having a file that is linked to the libacl.so shared library,
    possibly due to unusual output from a configure script.
    """
    # processUnmodified doesn't apply at all because this policy
    # does not walk packages
    requires = (
        # We don't want this policy to suggest anything already suggested
        # by EnforceSonameBuildRequirements
        ('EnforceSonameBuildRequirements', policy.CONDITIONAL_SUBSEQUENT),
    )
    regexp = r'^(\+ )?(%(cc)s|%(cxx)s|ld)( | .* )-l[a-zA-Z]+($| )'
    filetree = policy.NO_FILES

    def postInit(self):
        self.runnable = True
        self.warnedSoNames = set()
        self.logLines = []
        # subscribe to necessary build log entries
        if hasattr(self.recipe, 'subscribeLogs'):
            macros = {'cc': re.escape(self.recipe.macros.cc),
                      'cxx': re.escape(self.recipe.macros.cxx)}
            regexp = self.regexp % macros
            self.recipe.subscribeLogs(regexp)
            self.r = re.compile(regexp)
            macros = self.recipe.macros
            cfg = self.recipe.cfg
            self.libDirs = {'%s%s' %(cfg.root, macros.libdir): macros.libdir,
                            util.normpath('%s/%s'%(cfg.root, macros.lib)): '/%s' %macros.lib}
            self._initComponentExceptions()
        else:
            # disable this policy
            self.runnable = False

    def updateArgs(self, *args, **keywords):
        self.warnedSoNames = list(keywords.pop('warnedSoNames', set()))
        logLines = keywords.pop('logLines', [])
        if logLines:
            for line in logLines:
                if self.r.match(line):
                    self.logLines.append(line)
        _warnBuildRequirements.updateArgs(self, *args, **keywords)

    def test(self):
        if not self.runnable:
            return False
        if self.recipe.getSubscribeLogPath() is None:
            return False

        try:
            self.transitiveBuildRequires = self.recipe._getTransitiveBuildRequiresNames()
        except AttributeError:
            return False
        self.setTalk()
        return True

    def do(self):
        # For the purposes of this policy, the transitive buildRequires
        # includes suggestions already made for handling shared libraries,
        # since this policy is explicitly a fallback for the unusual
        # case of static linking outside of the package being built.
        transitiveBuildRequires = self.transitiveBuildRequires.union(self.warnedSoNames)
        cfg = self.recipe.cfg
        db = database.Database(cfg.root, cfg.dbPath)

        foundLibNames = set()
        allPossibleProviders = set()
        missingBuildRequires = set()
        self.buildDirLibNames = None
        destdir = self.recipe.macros.destdir
        builddir = self.recipe.macros.builddir
        tooManyChoices = {}
        noTroveFound = {}
        noLibraryFound = set()

        components = self.recipe.autopkg.components
        pathMap = self.recipe.autopkg.pathMap
        reqDepSet = deps.DependencySet()
        sharedLibraryRequires = set()
        for pkg in components.values():
            reqDepSet.union(pkg.requires)
        for dep in reqDepSet.iterDepsByClass(deps.SonameDependencies):
            soname = os.path.basename(dep.name).split('.')[0]
            sharedLibraryRequires.add(soname)
            if soname.startswith('lib'):
                sharedLibraryRequires.add(soname[3:])
            else:
                sharedLibraryRequires.add('lib%s' %soname)
        troveLibraries = set()
        for path in pathMap.iterkeys():
            basename = os.path.basename(path)
            if basename.startswith('lib') and basename.find('.') >= 0:
                troveLibraries.add(basename[3:].split('.')[0])

        self.recipe.synchronizeLogs()
        f = file(self.recipe.getSubscribeLogPath())

        libRe = re.compile('^-l[a-zA-Z]+$')
        libDirRe = re.compile('^-L/..*$')

        def logLineTokens():
            for logLine in f:
                logLine = logLine.strip()
                if not self.r.match(logLine):
                    continue
                yield logLine.split()
            for logLine in self.logLines:
                yield logLine.split()

        def pathSetToTroveSet(pathSet):
            troveSet = set()
            for path in pathSet:
                for pathReq in set(trove.getName()
                                   for trove in db.iterTrovesByPath(path)):
                    pathReqCandidates = _providesNames(pathReq)
                    # remove any recursive or non-existing buildreqs
                    pathReqCandidates = [x for x in pathReqCandidates 
                                         if db.hasTroveByName(x)]
                    if not pathReqCandidates:
                        continue
                    allPossibleProviders.update(pathReqCandidates)
                    # only the best option
                    pathReqCandidates = pathReqCandidates[0:1]
                    # now apply exceptions
                    pathReqCandidates = self._removeExceptionsFromList(
                        pathReqCandidates)
                    troveSet.add(pathReqCandidates[0])
            return troveSet

        def buildDirContains(libName):
            # If we can find this library built somewhere in the
            # builddir, chances are that the internal library is
            # what is being linked to in any case.
            if self.buildDirLibNames is None:
                # walk builddir once, the first time this is called
                self.buildDirLibNames = set()
                for dirpath, dirnames, filenames in os.walk(builddir):
                    for fileName in filenames:
                        if fileName.startswith('lib') and '.' in fileName:
                            self.buildDirLibNames.add(fileName[3:].split('.')[0])
            return libName in self.buildDirLibNames

        for tokens in logLineTokens():
            libNames = set(x[2:] for x in tokens if libRe.match(x))
            # Add to this set, for this line only, system library dirs,
            # nothing in destdir or builddir
            libDirs = self.libDirs.copy()
            for libDir in set(x[2:].rstrip('/') for x in tokens
                              if libDirRe.match(x) and
                                 not x[2:].startswith(destdir) and
                                 not x[2:].startswith(builddir)):
                libDir = util.normpath(libDir)
                libDirs.setdefault(util.normpath('%s%s' %(cfg.root, libDir)), libDir)
                libDirs.setdefault(libDir, libDir)
            for libName in sorted(list(libNames)):
                if libName not in foundLibNames:
                    if libName in sharedLibraryRequires:
                        foundLibNames.add(libName)
                        continue
                    if libName in troveLibraries:
                        foundLibNames.add(libName)
                        continue
                    if buildDirContains(libName):
                        foundLibNames.add(libName)
                        continue

                    foundLibs = set()
                    for libDirRoot, libDir in libDirs.iteritems():
                        for ext in ('a', 'so'):
                            # If there is no .a, look for the .so in case
                            # no shared library dependency is found from
                            # packaged files (CNP-132)
                            if util.exists('%s/lib%s.%s' %(libDirRoot, libName, ext)):
                                foundLibs.add('%s/lib%s.%s' %(libDir, libName, ext))
                                break
                    troveSet = pathSetToTroveSet(foundLibs)

                    if len(troveSet) == 1:
                        # found just one, we can confidently recommend it
                        recommended = list(troveSet)[0]
                        if recommended not in transitiveBuildRequires:
                            self.info("Add '%s' to buildRequires for -l%s (%s)",
                                      recommended, libName,
                                      ', '.join(sorted(list(foundLibs))))
                            missingBuildRequires.add(recommended)
                            foundLibNames.add(libName)

                    elif len(troveSet):
                        # found more, we might need to recommend a choice
                        tooManyChoices.setdefault(libName, [
                                  ' '.join(sorted(list(foundLibs))),
                                  "', '".join(sorted(list(troveSet)))])

                    elif foundLibs:
                        # found files on system, but no troves providing them
                        noTroveFound.setdefault(libName,
                                  ' '.join(sorted(list(foundLibs))))
                        
                    else:
                        # note that this does not prevent us from
                        # *looking* again, because the next time
                        # there might be a useful -L in the link line
                        noLibraryFound.add(libName)
                            
        if tooManyChoices:
            for libName in sorted(list(tooManyChoices.keys())):
                if libName not in foundLibNames:
                    # Found multiple choices for libName, and never came
                    # up with a better recommendation, so recommend a choice.
                    # Note: perhaps someday this can become an error
                    # when we have a better sense of how frequently
                    # it is wrong...
                    foundLibNames.add(libName)
                    foundLibs, troveSet = tooManyChoices[libName]
                    self.warn('Multiple troves match files %s for -l%s:'
                              ' choose one of the following entries'
                              " for buildRequires: '%s'",
                              foundLibs, libName, troveSet)

        if noTroveFound:
            for libName in sorted(list(noTroveFound.keys())):
                if libName not in foundLibNames:
                    # Never found any trove containing these libraries,
                    # not even a file in the builddir
                    foundLibNames.add(libName)
                    foundLibs = noTroveFound[libName]
                    self.info('No trove found matching any of files'
                              ' %s for -l%s:'
                              ' possible missing buildRequires',
                              foundLibs, libName)

        if noLibraryFound:
            for libName in sorted(list(noLibraryFound)):
                if libName not in foundLibNames:
                    # Note: perhaps someday this can become an error
                    # when we have a better sense of how frequently
                    # it is wrong...
                    self.info('No files found matching -l%s:'
                              ' possible missing buildRequires', libName)

        if missingBuildRequires:
            self.talk('add to buildRequires: %s',
                       str(sorted(list(missingBuildRequires))))
            reportMissingBuildRequires(self.recipe, missingBuildRequires)

        if allPossibleProviders:
            reportFoundBuildRequires(self.recipe, allPossibleProviders)

        f.close()


class EnforceLocalizationBuildRequirements(_warnBuildRequirements):
    """
    NAME
    ====

    B{C{r.EnforceLocalizationBuildRequirements()}} - Ensures that
    internationalization tools are required by packages that have
    C{POTFILES.in} files in their source archives.

    SYNOPSIS
    ========

    C{r.EnforceLocalizationBuildRequirements([I{exceptions='path/to/POTFILES.in'}])}

    DESCRIPTION
    ===========

    The C{r.EnforceLocalizationBuildRequirements()} policy ensures
    that internationalization tools are included in the build
    requirements if a C{POTFILES.in} file is found.

    EXAMPLES
    ========

    C{r.EnforceLocalizationBuildRequirements(exceptions='.*')
    
    Since this policy is essentially binary -- you either find one
    or more C{POTFILES.in} or you don't find any -- the only
    reasonable exception is if there are some C{POTFILES.in}
    files that you know are not used or that you do not wish
    to use, you can disable this policy.
    """
    # processUnmodified doesn't apply at all because this policy
    # does not walk packages
    filetree = policy.BUILDDIR
    invariantinclusions = [ (r'.*/POTFILES\.in', 0400, stat.S_IFDIR), ]
    intltools = set(('gettext:runtime', 'intltool:runtime'))
    runOnce = False

    def doFile(self, path):
        if self.runOnce:
            return
        self.runOnce = True

        reportFoundBuildRequires(self.recipe, self.intltools)
        transitiveBuildRequires = self.recipe._getTransitiveBuildRequiresNames()
        foundReqs = self.intltools.intersection(transitiveBuildRequires)
        if foundReqs != self.intltools:
            missingReqs = self.intltools - foundReqs
            self.warn('missing buildRequires %s for file %s',
                      str(sorted(list(missingReqs))), path[1:])
            reportMissingBuildRequires(self.recipe, missingReqs)
