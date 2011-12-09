#!/usr/bin/env python
# encoding: utf-8
"""
process.py

Created by Thomas Mangin on 2011-11-29.
Copyright (c) 2011 Exa Networks. All rights reserved.
"""

from threading import Thread
import subprocess
import errno

import os
import time
import socket

from Queue import Empty

from .http import regex

from .logger import Logger
logger = Logger()

from .configuration import Configuration
configuration = Configuration()

class Worker (Thread):
	
	# TODO : if the program is a function, fork and run :)
	
	def __init__ (self, name, request_box, program):
		self.wid = name                               # a unique name
		self.creation = time.time()                   # when the thread was created
		self.last_worked = self.creation              # when the thread last picked a task
		self.request_box = request_box                # queue with HTTP headers to process

		r, w = os.pipe()                              # pipe for communication with the main thread
		self.response_box_write = w                   # results are written here
		self.response_box = r                         # read from the main thread

		self.program = program                        # the squid redirector program to fork 
		self.running = True                           # the thread is active
		Thread.__init__(self)

	def _init (self):
		try:
			self.process = subprocess.Popen([self.program,],
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				universal_newlines=True,
			)
			logger.worker('spawn process %s' % self.program, 'worker %d' % self.wid)
			return True
		except KeyboardInterrupt:
			return False
		except (subprocess.CalledProcessError,OSError,ValueError):
			logger.worker('could not spawn process %s' % self.program, 'worker %d' % self.wid)
			return False
	
	def _cleanup (self):
		logger.worker('terminating process', 'worker %d' % self.wid)
		try:
			self.process.terminate()
			self.process.wait()
		except OSError, e:
			# No such processs
			if e[0] != errno.ESRCH:
				logger.worker('PID %s died' % pid, 'worker %d' % self.wid)

	def stop (self):
		self.running = False

	def run (self):
		if not self.running:
			logger.worker('can not start', 'worker %d' % self.wid)
			return
		logger.worker('starting', 'worker %d' % self.wid)
		self.running = self._init()
		while self.running:
			try:
				cid,peer,request = self.request_box.get(timeout=1)
			except Empty:
				continue
			logger.worker('some work came', 'worker %d' % self.wid)
			logger.worker('peer %s' % str(peer), 'worker %d' % self.wid)
			logger.worker('request %s' % ' '.join(request.split('\n',3)[:2]), 'worker %d' % self.wid)

			r = regex.destination.match(request)
			if not r:
				# XXX: send some http error
				#self.response_box_write.put()
				continue

			# XXX: we want to have these returned to us rather than knowing
			# XXX: which group indexes we're interested in
			method = r.groups()[0]
			path = r.groups()[2]
			host = r.groups()[4]

			# Do the hostname resolution before the backend check
			# We may block the page but filling the OS DNS cache can not harm :)
			try:
				#raise socket.error('UNCOMMENT TO TEST DNS RESOLUTION FAILURE')
				ip = socket.gethostbyname(host)
			except socket.error,e:
				logger.worker('could not resolve %s' % host, 'worker %d' % self.wid)
				self.response_box_write.write('%s %s %s %d %s\n' % (cid,'response','-',0,'NO_DNS'))
				self.response_box_write.flush()
				continue

			if path.startswith('http://'):
				url = path
			else:
				url = 'http://' + host + path
			r = regex.x_forwarded_for.match(request)
			client = r.group(0) if r else '0.0.0.0'
	
			squid = '%s %s - %s -' % (url,client,method)
			##logger.worker('sending to classifier : [%s]' % squid, 'worker %d' % self.wid)
			try:
				self.process.stdin.write('%s%s' % (squid,os.linesep))
				self.process.stdin.flush()
				response = self.process.stdout.readline()
			except IOError,e:
				logger.worker('IO/Error when sending to process, %s' % str(e), 'worker %d' % self.wid)
				# XXX: Do something
				return
			logger.worker('received from classifier : [%s]' % response.strip(), 'worker %d' % self.wid)
			if response == '\n':
				response = host

			logger.worker('need to download data on %s at %s' % (host,ip), 'worker %d' % self.wid)
			self.response_box_write.write('%s %s %s %d %s\n' % (cid,'request',ip,80,request.replace('\n','\\n').replace('\r','\\r')))
			self.response_box_write.flush()
			##logger.worker('[%s %s %s %d %s]' % (cid,'request',ip,80,request), 'worker %d' % self.wid)
			self.last_worked = time.time()
			logger.worker('waiting for some work', 'worker %d' % self.wid)
		self._cleanup()
	
