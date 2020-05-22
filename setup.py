import setuptools
with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name='adbproxy',
    version='1.0.4',
    author="Paulo Costa",
    author_email="me@paulo.costa.nom.br",
    description="Creates an ADB bridge between your computer and a device somewhere else",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/paulo-raca/adb-proxy",
    packages=setuptools.find_packages(),
    entry_points={
        'console_scripts': [
            'adbproxy=adbproxy:main_sync'
        ],
    },
    include_package_data=True,
    package_data={
        'adbproxy': [
            'dummy.apk',
        ],
    },
    install_requires=[
        "aioboto3>=6.4.1",
        "aiohttp>=3.6.2",
        "asyncssh>=2.1.0",
        "argcomplete>=1.10.3",
        "uri>=2.0.1",
        "miniupnpc>=2.0.2",
        "PyYAML>=5.1.2",
    ],
    classifiers=[
        "Topic :: Internet :: Proxy Servers",
        "Intended Audience :: Developers",
        "Environment :: Console",
        "Programming Language :: Python :: 3.6",
        "Operating System :: Android",
    ],
)
