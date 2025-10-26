from setuptools import setup, find_packages
from pathlib import Path

# Read README for long description
readme = Path(__file__).parent / "README.md"
long_description = readme.read_text() if readme.exists() else ""

setup(
    name='kube-borg-backup-cli',
    version='6.0.0',  # Match restore feature version
    author='Frederik Berg',
    description='CLI tool for kube-borg-backup restore operations',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/frederikb96/kube-borg-backup',
    packages=find_packages(),
    install_requires=[
        'kubernetes>=28.0.0',
        'PyYAML>=6.0',
    ],
    entry_points={
        'console_scripts': [
            'kbb=kbb.main:main',
        ],
    },
    python_requires='>=3.11',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
    ],
)
