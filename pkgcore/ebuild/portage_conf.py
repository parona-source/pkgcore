# Copyright: 2006 Brian Harring <ferringb@gmail.com>
# License: GPL2

"""make.conf translator.

Converts portage configuration files into L{pkgcore.config} form.
"""

import os
from pkgcore.config import basics, configurable
from pkgcore import const
from pkgcore.ebuild import const as ebuild_const
from pkgcore.util.demandload import demandload
demandload(globals(), "errno pkgcore.config:errors "
    "pkgcore.pkgsets.glsa:SecurityUpgrades "
    "pkgcore.fs.util:normpath,abspath "
    "pkgcore.util.file:read_bash_dict,read_dict "
    "pkgcore.util.osutils:listdir_files ")


def my_convert_hybrid(manager, val, arg_type):
    """Modified convert_hybrid using a sequence of strings for section_refs."""
    if arg_type == 'section_refs':
        return list(manager.collapse_named_section(name) for name in val)
    return basics.convert_hybrid(manager, val, arg_type)


@configurable({'ebuild_repo': 'ref:repo', 'vdb': 'ref:repo',
               'profile': 'ref:profile'})
def SecurityUpgradesViaProfile(ebuild_repo, vdb, profile):
    """
    generate a GLSA vuln. pkgset limited by profile

    @param ebuild_repo: L{pkgcore.ebuild.repository.UnconfiguredTree} instance
    @param vdb: L{pkgcore.repository.prototype.tree} instance that is the livefs
    @param profile: L{pkgcore.ebuild.profiles} instance
    """
    arch = profile.conf.get("ARCH")
    if arch is None:
        raise errors.InstantiationError("arch wasn't set in profiles")
    return SecurityUpgrades(ebuild_repo, vdb, arch)


@configurable({'location': 'str'}, typename='configsection')
def config_from_make_conf(location="/etc/"):
    """
    generate a config from a file location

    @param location: location the portage configuration is based in,
        defaults to /etc
    """

    # this actually differs from portage parsing- we allow
    # make.globals to provide vars used in make.conf, portage keeps
    # them seperate (kind of annoying)

    pjoin = os.path.join

    config_root = os.environ.get("CONFIG_ROOT", "/")
    base_path = pjoin(config_root, location.strip("/"))
    portage_base = pjoin(base_path, "portage")

    # this isn't preserving incremental behaviour for features/use
    # unfortunately
    conf_dict = read_bash_dict(pjoin(base_path, "make.globals"))
    conf_dict.update(read_bash_dict(
            pjoin(base_path, "make.conf"), vars_dict=conf_dict,
            sourcing_command="source"))
    conf_dict.setdefault("PORTDIR", "/usr/portage")
    root = os.environ.get("ROOT", conf_dict.get("ROOT", "/"))
    gentoo_mirrors = list(
        x+"/distfiles" for x in conf_dict.pop("GENTOO_MIRRORS", "").split())
    if not gentoo_mirrors:
        gentoo_mirrors = None

    features = conf_dict.get("FEATURES", "").split()

    new_config = {}

    # sets...
    new_config["world"] = basics.AutoConfigSection({
            "class": "pkgcore.pkgsets.filelist.FileList",
            "location": pjoin(root, const.WORLD_FILE)})
    new_config["system"] = basics.AutoConfigSection({
            "class": "pkgcore.pkgsets.system.SystemSet",
            "profile": "profile"})

    set_fp = pjoin(portage_base, "sets")
    if os.path.isdir(set_fp):
        for setname in listdir_files(set_fp):
            # Potential for name clashes here, those will just make
            # the set not show up in config.
            new_config[setname] = basics.AutoConfigSection({
                    "class":"pkgcore.pkgsets.filelist.FileList",
                    "location":pjoin(set_fp, setname)})

    new_config["vdb"] = basics.AutoConfigSection({
            "class": "pkgcore.vdb.repository",
            "location": pjoin(config_root, 'var', 'db', 'pkg')})

    make_profile = pjoin(base_path, 'make.profile')
    try:
        profile = normpath(abspath(pjoin(
                    base_path, os.readlink(make_profile))))
    except OSError, oe:
        if oe.errno in (errno.ENOENT, errno.EINVAL):
            raise errors.InstantiationError(
                "%s must be a symlink pointing to a real target" % (
                    make_profile,))
        raise errors.InstantiationError(
            "%s: unexepect error- %s" % (make_profile, oe.strerror))

    psplit = list(piece for piece in profile.split(os.path.sep) if piece)
    # poor mans rindex.
    for i, piece in enumerate(reversed(psplit)):
        if piece == 'profiles':
            break
    else:
        raise errors.InstantiationError(
            '%s expands to %s, but no profiles base detected' % (
                pjoin(base_path, 'make.profile'), profile))
    if not i:
        raise errors.InstantiationError(
            '%s expands to %s, but no profile detected' % (
                pjoin(base_path, 'make.profile'), profile))

    new_config["profile"] = basics.AutoConfigSection({
            "class": "pkgcore.ebuild.profiles.OnDiskProfile",
            "base_path": pjoin("/", *psplit[:-i]),
            "profile": pjoin(*psplit[-i:])})

    portdir = normpath(conf_dict.pop("PORTDIR").strip())
    portdir_overlays = [
        normpath(x) for x in conf_dict.pop("PORTDIR_OVERLAY", "").split()]

    #fetcher.
    distdir = normpath(conf_dict.pop("DISTDIR", pjoin(portdir, "distdir")))
    fetchcommand = conf_dict.pop("FETCHCOMMAND")
    resumecommand = conf_dict.pop("RESUMECOMMAND", fetchcommand)

    new_config["fetcher"] = basics.AutoConfigSection({
            "class": "pkgcore.fetch.custom.fetcher",
            "distdir": distdir,
            "command": fetchcommand,
            "resume_command": resumecommand})

    # define the eclasses now.
    all_ecs = []
    for x in [portdir] + portdir_overlays:
        ec_path = pjoin(x, "eclass")
        new_config[ec_path] = basics.AutoConfigSection({
                "class": "pkgcore.ebuild.eclass_cache.cache",
                "path": ec_path,
                "portdir": portdir})
        all_ecs.append(ec_path)

    new_config['ebuild-repo-common'] = basics.AutoConfigSection({
            'class': 'pkgcore.ebuild.repository.tree',
            'default_mirrors': gentoo_mirrors,
            'eclass_cache': 'eclass stack'})
    new_config['cache-common'] = basics.AutoConfigSection({
            'class': 'pkgcore.cache.flat_hash.database',
            'auxdbkeys': ebuild_const.metadata_keys,
            'location': pjoin(config_root, 'var', 'cache', 'edb', 'dep'),
            })

    for tree_loc in portdir_overlays:
        new_config[tree_loc] = basics.AutoConfigSection({
                'inherit': ('ebuild-repo-common',),
                'location': tree_loc,
                'cache': (basics.AutoConfigSection({
                            'inherit': ('cache-common',),
                            'label': tree_loc}),),
                })

    rsync_portdir_cache = os.path.exists(pjoin(portdir, "metadata", "cache")) \
        and "metadata-transfer" not in features

    # if a metadata cache exists, use it
    if rsync_portdir_cache:
        new_config["portdir cache"] = basics.AutoConfigSection({
                'class': 'pkgcore.cache.metadata.database',
                'location': portdir,
                'label': 'portdir cache',
                'auxdbkeys': ebuild_const.metadata_keys})
    else:
        new_config["portdir cache"] = basics.AutoConfigSection({
                'inherit': ('cache-common',),
                'label': portdir})

    # setup portdir.
    cache = ('portdir cache',)
    if not portdir_overlays:
        new_config[portdir] = basics.DictConfigSection(my_convert_hybrid, {
                'inherit': ('ebuild-repo-common',),
                'location': portdir,
                'cache': ('portdir cache',)})
        new_config["eclass stack"] = basics.section_alias(
            pjoin(portdir, 'eclass'), 'eclass_cache')
        new_config['portdir'] = basics.section_alias(portdir, 'repo')
        new_config['repo-stack'] = basics.section_alias(portdir, 'repo')
    else:
        # There's always at least one (portdir) so this means len(all_ecs) > 1
        new_config['%s cache' % (portdir,)] = basics.AutoConfigSection({
                'inherit': ('cache-common',),
                'label': portdir})
        cache = ('portdir cache',)
        if rsync_portdir_cache:
            cache = ('%s cache' % (portdir,),) + cache
        new_config[portdir] = basics.DictConfigSection(my_convert_hybrid, {
                'inherit': ('ebuild-repo-common',),
                'location': portdir,
                'cache': cache})

        if rsync_portdir_cache:
            # created higher up; two caches, writes to the local,
            # reads (when possible) from pregenned metadata
            cache = ('portdir cache',)
        else:
            cache = ('%s cache' % (portdir,),)
        new_config['portdir'] = basics.DictConfigSection(my_convert_hybrid, {
                'inherit': ('ebuild-repo-common',),
                'location': portdir,
                'cache': cache,
                'eclass_cache': pjoin(portdir, 'eclass')})

        # reverse the ordering so that overlays override portdir
        # (portage default)
        new_config["eclass stack"] = basics.DictConfigSection(
            my_convert_hybrid, {
                'class': 'pkgcore.ebuild.eclass_cache.StackedCaches',
                'caches': tuple(reversed(all_ecs))})

        new_config['repo-stack'] = basics.DictConfigSection(my_convert_hybrid,
            {'class': 'pkgcore.ebuild.overlay_repository.OverlayRepo',
             'trees': tuple([portdir] + portdir_overlays)})

    # XXX I have nfc what all this is about

#     if os.path.exists(base_path+"portage/modules"):
#         pcache = read_dict(
#             base_path+"portage/modules").get("portdbapi.auxdbmodule", None)

#        cache_config = {"type": "cache",
#                        "location": "%s/var/cache/edb/dep" %
#                           config_root.rstrip("/"),
#                        "label": "make_conf_overlay_cache"}
#        if pcache is None:
#            if portdir_overlays or ("metadata-transfer" not in features):
#                cache_config["class"] = "pkgcore.cache.flat_hash.database"
#            else:
#                cache_config["class"] = "pkgcore.cache.metadata.database"
#                cache_config["location"] = portdir
#        	 cache_config["readonly"] = "true"
#        else:
#            cache_config["class"] = pcache
#
#        new_config["cache"] = basics.ConfigSectionFromStringDict(
#            "cache", cache_config)


    new_config['glsa'] = basics.AutoConfigSection({
            'class': SecurityUpgradesViaProfile,
            'ebuild_repo': 'repo-stack',
            'vdb': 'vdb',
            'profile': 'profile'})

    #binpkg.
    pkgdir = conf_dict.pop('PKGDIR', None)
    default_repos = ('repo-stack',)
    if pkgdir is not None:
        try:
            pkgdir = abspath(pkgdir)
        except OSError, oe:
            if oe.errno != errno.ENOENT:
                raise
            pkgdir = None
        if pkgdir and os.path.isdir(pkgdir):
            new_config['binpkg'] = basics.ConfigSectionFromStringDict({
                    'class': 'pkgcore.binpkg.repository.tree',
                    'location': pkgdir})
            default_repos += ('binpkg',)

    # finally... domain.
    conf_dict.update({
            'class': 'pkgcore.ebuild.domain.domain',
            'repositories': default_repos,
            'fetcher': 'fetcher',
            'default': True,
            'vdb': ('vdb',),
            'profile': 'profile',
            'name': 'livefs domain'})
    for f in (
        "package.mask", "package.unmask", "package.keywords", "package.use"):
        fp = pjoin(portage_base, f)
        if os.path.isfile(fp):
            conf_dict[f] = fp
    new_config['livefs domain'] = basics.DictConfigSection(my_convert_hybrid,
                                                           conf_dict)

    return new_config
