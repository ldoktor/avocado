# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright: Red Hat Inc. 2013-2014
# Author: Lucas Meneghel Rodrigues <lmr@redhat.com>

"""
Module with network related utility functions
"""

import socket
import random

from six.moves import xrange as range

from .data_structures import Borg

#: Families taken into account in this class
FAMILIES = (socket.AF_INET, socket.AF_INET6)


def is_port_free(port, address):
    """
    Return True if the given port is available for use.

    Currently we only check for TCP connections on ipv4/6

    :param port: Port number
    :param address: Socket address to bind or connect
    """
    s = None
    try:
        for family in FAMILIES:
            try:
                s = socket.socket(family, socket.SOCK_STREAM)
                if address == "localhost":
                    s.bind((address, port))
                else:
                    s.connect((address, port))
                    return False
            except socket.error:
                if address == "localhost":
                    return False
            s.close()
        return True
    finally:
        if s is not None:
            s.close()


def find_free_port(start_port=1024, end_port=65535, address="localhost", sequent=True):
    """
    Return a host free port in the range [start_port, end_port].

    :param start_port: header of candidate port range, defaults to 1024
    :param end_port: ender of candidate port range, defaults to 65535
    :param sequent: Find port sequentially, random order if it's False
    :param address: Socket address to bind or connect
    :rtype: int or None if no free port found
    """
    port_range = range(start_port, end_port)
    if not sequent:
        port_range = list(port_range)
        random.shuffle(port_range)
    for i in port_range:
        if is_port_free(i, address):
            return i
    return None


def find_free_ports(start_port, end_port, count, address="localhost", sequent=True):
    """
    Return count of host free ports in the range [start_port, end_port].

    :param start_port: header of candidate port range
    :param end_port: ender of candidate port range
    :param count: Initial number of ports known to be free in the range.
    :param address: Socket address to bind or connect
    :param sequent: Find port sequentially, random order if it's False
    """
    ports = []

    port_range = range(start_port, end_port)
    if not sequent:
        port_range = list(port_range)
        random.shuffle(port_range)
    for i in port_range:
        if is_port_free(i, address):
            ports.append(i)
        if len(ports) >= count:
            break

    return ports


class PortTracker(Borg):

    """
    Tracks ports used in the host machine.
    """

    def __init__(self):
        Borg.__init__(self)
        self.address = 'localhost'
        self.start_port = 5000
        if not hasattr(self, 'retained_ports'):
            self._reset_retained_ports()

    def __str__(self):
        return 'Ports tracked: %r' % self.retained_ports

    def _reset_retained_ports(self):
        self.retained_ports = []

    def register_port(self, port):
        if (port not in self.retained_ports) and is_port_free(port, self.address):
            self.retained_ports.append(port)
        else:
            raise ValueError('Port %d in use' % port)
        return port

    def find_free_port(self, start_port=None):
        if start_port is None:
            start_port = self.start_port
        port = start_port
        while ((port in self.retained_ports) or
               (not is_port_free(port, self.address))):
            port += 1
        self.retained_ports.append(port)
        return port

    def release_port(self, port):
        if port in self.retained_ports:
            self.retained_ports.remove(port)
