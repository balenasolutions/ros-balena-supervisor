"""Launch the balena supervisor node with parameters from config/params.yaml."""

from __future__ import annotations

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    default_params = os.path.join(
        get_package_share_directory('balena_supervisor_node'),
        'config', 'params.yaml')

    params_file = LaunchConfiguration('params_file')
    namespace = LaunchConfiguration('namespace')

    return LaunchDescription([
        DeclareLaunchArgument(
            'params_file', default_value=default_params,
            description='Parameter file for the balena supervisor node.'),
        DeclareLaunchArgument(
            'namespace', default_value='',
            description='Namespace to push the node into.'),
        Node(
            package='balena_supervisor_node',
            executable='balena_supervisor_node',
            name='balena_supervisor',
            namespace=namespace,
            parameters=[params_file],
            output='screen',
            emulate_tty=True,
        ),
    ])
