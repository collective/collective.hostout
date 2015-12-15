##############################################################################
#
# Copyright (c) 2006 Zope Corporation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################

import os
from setuptools import setup, find_packages

def read(*rnames):
    return open(os.path.join(os.path.dirname(__file__), *rnames)).read()

name = "collective.hostout"
setup(
    name = name,
    version = "1.1",
    author = "Dylan Jay",
    author_email = "software@pretaweb.com",
    description = """standardized deployment of zc.buildout based applications with Fabric""",
    license = "GPL",
    keywords = "buildout, zc.buildout, fabric, deploy, deployment, server, plone, django, host, hosting",
    classifiers = [
        'Framework :: Buildout',
        "Development Status :: 3 - Alpha",
        "Topic :: Utilities",],
    url='http://github.com/collective/'+name,
    long_description=(
        read('README.rst')
        + '\n' +
        read('CHANGES.txt')
        + '\n' 
        ),

    packages = find_packages(),
    include_package_data = True,
#    data_files = [('.', ['*.txt'])],
#    package_data = {'':('*.txt')},
    namespace_packages = ['collective'],
    install_requires = [
                        'zc.recipe.egg',
                        'setuptools',
#                        'Fabric>=1.0',
                        'Fabric',
# These need to be added to the python that buildout uses in order to build packages properly #TODO How to automate?
#                        'setuptools_subversion',
#                        'setuptools-git',

#                        'socksproxy',
#                        'collective.eggproxy',
                        ],
    entry_points = {'zc.buildout': ['default = collective.hostout:Recipe',],
                    'console_scripts': ['hostout = collective.hostout.hostout:main'],
                    'fabric': ['fabfile = collective.hostout.fabfile']
                    },
    zip_safe = False,
    )
