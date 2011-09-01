# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Module that handles interactions with a Validation Pool.

The validation pool is the set of commits that are ready to be validated i.e.
ready for the commit queue to try.
"""

import json
import logging
import time
import urllib
from xml.dom import minidom

from chromite.buildbot import gerrit_helper
from chromite.buildbot import lkgm_manager
from chromite.buildbot import patch as cros_patch
from chromite.lib import cros_build_lib


class TreeIsClosedException(Exception):
  """Raised when the tree is closed and we wanted to submit changes."""

  def __init__(self):
    super(TreeIsClosedException, self).__init__(
        'TREE IS CLOSED.  PLEASE SET TO OPEN OR THROTTLED TO COMMIT')


class ValidationPool(object):
  """Class that handles interactions with a validation pool.

  This class can be used to acquire a set of commits that form a pool of
  commits ready to be validated and committed.

  Usage:  Use ValidationPoo.AcquirePool -- a static
  method that grabs the commits that are ready for validation.
  """

  GLOBAL_DRYRUN = False

  def __init__(self, dryrun):
    """Initializes an instance by setting default valuables to instance vars.

    Generally use AcquirePool as an entry pool to a pool rather than this
    method.

    Args:
      dryrun: If set to True, do not submit anything to Gerrit.
    """
    self.changes = []
    self.gerrit_helper = None
    self.dryrun = dryrun | self.GLOBAL_DRYRUN

  @classmethod
  def _IsTreeOpen(cls):
    """Returns True if the tree is open or throttled."""

    def _SleepWithExponentialBackOff(current_sleep):
      """Helper function to sleep with exponential backoff."""
      time.sleep(current_sleep)
      return current_sleep * 2

    max_attempts = 5
    response_dict = None
    status_url = 'https://chromiumos-status.appspot.com/current?format=json'
    current_sleep = 1

    for attempt in range(max_attempts):
      try:
        response = urllib.urlopen(status_url)
        # Check for valid response code.
        if response.getcode() == 200:
          response_dict = json.load(response)
          break

        current_sleep = _SleepWithExponentialBackOff(current_sleep)
      except IOError:
        # We continue if we can't reach appspot.com
        current_sleep = _SleepWithExponentialBackOff(current_sleep)

    else:
      # We go ahead and say the tree is open if we can't tree the status page.
      logging.warn('Could not get a status from %s', status_url)
      return True

    if attempt > 1:
      logging.warn('Had to attempt to reach %s %d times', status_url, attempt)

    tree_open = response_dict['general_state'] in ['open', 'throttled']
    return tree_open

  @classmethod
  def AcquirePool(cls, branch, internal, buildroot, dryrun):
    """Acquires the current pool from Gerrit.

    Polls Gerrit and checks for which change's are ready to be committed.

    Args:
      branch: The branch for the validation pool.
      internal: If True, use gerrit-int.
      buildroot: The location of the buildroot used to filter projects.
      dryrun: Don't submit anything to gerrit.
    Returns:
      ValidationPool object.
    Raises:
      TreeIsClosedException: if the tree is closed.
    """
    if cls._IsTreeOpen():
      pool = ValidationPool(dryrun)
      pool.gerrit_helper = gerrit_helper.GerritHelper(internal)
      raw_changes = pool.gerrit_helper.GrabChangesReadyForCommit(branch)
      pool.changes = pool.gerrit_helper.FilterProjectsNotInSourceTree(
          raw_changes, buildroot)
      return pool
    else:
      raise TreeIsClosedException()

  @classmethod
  def AcquirePoolFromManifest(cls, manifest, internal):
    """Acquires the current pool from a given manifest.

    Args:
      manifest: path to the manifest where the pool resides.
      internal: if true, assume gerrit-int.
    Returns:
      ValidationPool object.
    """
    pool = ValidationPool()
    pool.gerrit_helper = gerrit_helper.GerritHelper(internal)
    manifest_dom = minidom.parse(manifest)
    pending_commits = manifest_dom.getElementsByTagName(
        lkgm_manager.PALADIN_COMMIT_ELEMENT)
    for pending_commit in pending_commits:
      project = pending_commit.getAttribute(lkgm_manager.PALADIN_PROJECT_ATTR)
      change = pending_commit.getAttribute(lkgm_manager.PALADIN_CHANGE_ID_ATTR)
      commit = pending_commit.getAttribute(lkgm_manager.PALADIN_COMMIT_ATTR)
      pool.changes.append(pool.gerrit_helper.GrabPatchFromGerrit(
          project, change, commit))

    return pool

  def ApplyPoolIntoRepo(self, directory):
    """Cherry picks changes from pool into repository.

    Returns:
      True if we managed to apply some changes.
    """
    non_applied_changes = []
    for change in self.changes:
      try:
        change.Apply(directory, trivial=True)
      except cros_patch.ApplyPatchException:
        non_applied_changes.append(change)
      else:
        lkgm_manager.PrintLink(str(change), change.url)

    if non_applied_changes:
      logging.debug('Some changes could not be applied cleanly.')
      self.HandleApplicationFailure(non_applied_changes)
      self.changes = list(set(self.changes) - set(non_applied_changes))

    return len(self.changes) > 0

  def SubmitPool(self):
    """Commits changes to Gerrit from Pool.

    Returns:
      True if we were able to do submit the changes.
    Raises:
      TreeIsClosedException: if the tree is closed.
    """
    if ValidationPool._IsTreeOpen():
      for change in self.changes:
        logging.info('Change %s will be submitted', change)
        try:
          change.Submit(self.gerrit_helper, dryrun=self.dryrun)
        except cros_build_lib.RunCommandError:
          change.HandleCouldNotSubmit(self.gerrit_helper,
                                      dryrun=self.dryrun)
          # TODO(sosa): Do we re-raise?
        return True
    else:
      raise TreeIsClosedException()

  def HandleApplicationFailure(self, changes):
    """Handles changes that were not able to be applied cleanly."""
    for change in changes:
      logging.info('Change %s did not apply cleanly.', change)
      change.HandleCouldNotApply(self.gerrit_helper, dryrun=self.dryrun)

  def HandleValidationFailure(self):
    """Handles failed changes by removing them from next Validation Pools."""
    logging.info('Validation failed for all changes.')
    for change in self.changes:
      logging.info('Validation failed for change %s.', change)
      change.HandleCouldNotVerify(self.gerrit_helper, dryrun=self.dryrun)

