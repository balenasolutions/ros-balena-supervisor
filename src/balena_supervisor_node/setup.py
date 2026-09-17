from glob import glob

from setuptools import find_packages, setup

package_name = 'balena_supervisor_node'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
         ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
        ('share/' + package_name + '/config', glob('config/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Sam Duffield',
    maintainer_email='sam.duffield@balena.io',
    description='ROS 2 node exposing the on-device balena supervisor API.',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'balena_supervisor_node = balena_supervisor_node.node:main',
        ],
    },
)
