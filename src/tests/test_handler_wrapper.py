# pylint: disable=protected-access

import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from cfn_resource import ProgressEvent, Status
from cfn_resource import exceptions
from cfn_resource.handler_wrapper import _handler_wrapper, HandlerWrapper
from cfn_resource.base_resource_model import BaseResourceModel as ResourceModel

PARENT = Path(__file__).parent
EVENTS = {
    "SYNC-GOOD": [
        PARENT / "data" / "create.request.json",
        PARENT / "data" / "delete.request.json",
        PARENT / "data" / "list.request.json",
        PARENT / "data" / "read.request.json",
        PARENT / "data" / "update.request.json"
    ],
    "ASYNC-GOOD": [
        PARENT / "data" / "create.with-request-context.request.json",
        PARENT / "data" / "delete.with-request-context.request.json",
        PARENT / "data" / "update.with-request-context.request.json",
    ],
    "BAD": [
        PARENT / "data" / "missing-fields.request.json",
    ]
}

sys.path.append(str(PARENT))

SIMPLE_PROGRESS_EVENT = ProgressEvent(status=Status.SUCCESS, resourceModel=ResourceModel())


def _get_event(evt_path):
    with open(evt_path, 'r') as file_h:
        event = json.load(file_h)
    return event


def mock_remaining_time(val=None):
    if not val:
        val = 9000

    def remaining():
        return val

    return remaining


class MockContext:

    def __init__(self, val=None):
        self.get_remaining_time_in_millis = mock_remaining_time(val)
        self.invoked_function_arn = 'arn:aws:lambda:us-west-2:123412341234:function:my-function'


def mock_handler(*args, **kwargs):  # pylint: disable=unused-argument
    return SIMPLE_PROGRESS_EVENT


def mock_handler_reschedule(*args, **kwargs):  # pylint: disable=unused-argument
    return ProgressEvent(
        status=Status.IN_PROGRESS,
        resourceModel=ResourceModel(),
        callbackContext={"some_key": "some-value"},
        callbackDelayMinutes=1
    )


class TestHandlerWrapper(unittest.TestCase):

    @mock.patch('cfn_resource.handler_wrapper.HandlerWrapper.__init__', return_value=None)
    @mock.patch('cfn_resource.handler_wrapper.HandlerWrapper.run_handler', return_value=SIMPLE_PROGRESS_EVENT)
    def test_handler_wrapper_func(self, mock_hw_run_handler, mock_hw_init):
        for event in EVENTS['SYNC-GOOD']:
            mock_hw_init.reset_mock()
            mock_hw_run_handler.reset_mock()

            event = _get_event(event)
            resp = _handler_wrapper(event, MockContext())
            resp = json.loads(resp)
            self.assertEqual('SUCCESS', resp['status'])
            mock_hw_init.assert_called_once()
            mock_hw_run_handler.assert_called_once()

        for event in EVENTS['BAD']:
            mock_hw_init.reset_mock()
            mock_hw_run_handler.reset_mock()

            event = _get_event(event)
            resp = _handler_wrapper(event, MockContext())
            resp = json.loads(resp)
            self.assertEqual('FAILED', resp['status'])
            self.assertEqual('InternalFailure', resp['errorCode'])

    def test_handler_wrapper_get_handler(self):
        for event in EVENTS["SYNC-GOOD"]:
            h_wrap = HandlerWrapper(_get_event(event), MockContext())
            handler = h_wrap._get_handler('mock_handler')
            self.assertEqual(True, isinstance(handler, types.FunctionType))

        h_wrap = HandlerWrapper(_get_event(EVENTS["SYNC-GOOD"][0]), MockContext())

        with self.assertRaises(exceptions.InternalFailure):
            h_wrap._get_handler('non-existant-module')

        with self.assertRaises(exceptions.InternalFailure):
            h_wrap._action = 'nonexistant'
            h_wrap._get_handler('cfn_resource.mock_handler')

    @mock.patch('cfn_resource.handler_wrapper.HandlerWrapper._get_handler', return_value=mock_handler)
    @mock.patch('cfn_resource.metrics.Metrics.publish')
    def test_run_handler_good(self, mock_metric_publish, mock_get_handler):
        for event in EVENTS["SYNC-GOOD"]:
            mock_metric_publish.reset_mock()
            mock_get_handler.reset_mock()
            h_wrap = HandlerWrapper(_get_event(event), MockContext())
            resp = h_wrap.run_handler()
            mock_get_handler.assert_called_once()
            mock_metric_publish.assert_called_once()
            self.assertEqual(Status.SUCCESS, resp.status)

    @mock.patch('cfn_resource.handler_wrapper.HandlerWrapper._get_handler', return_value=mock_handler_reschedule)
    @mock.patch('cfn_resource.metrics.Metrics.publish')
    @mock.patch('cfn_resource.scheduler.CloudWatchScheduler.reschedule', return_value=None)
    def test_good_run_handler_reschedule(self, mock_scheduler, mock_metric_publish, mock_get_handler):
        h_wrap = HandlerWrapper(_get_event(EVENTS["SYNC-GOOD"][0]), MockContext())
        resp = h_wrap.run_handler()
        print(resp.json())
        mock_get_handler.assert_called_once()
        mock_metric_publish.assert_called_once()
        mock_scheduler.assert_called_once()
        print(resp.json())
        self.assertEqual(Status.IN_PROGRESS, resp.status)

    @mock.patch('cfn_resource.handler_wrapper.HandlerWrapper._get_handler', return_value=mock_handler)
    @mock.patch('cfn_resource.metrics.Metrics.publish')
    def test_good_run_handler_sam_local(self, mock_metric_publish, mock_get_handler):
        os.environ['AWS_SAM_LOCAL'] = 'true'
        h_wrap = HandlerWrapper(_get_event(EVENTS["SYNC-GOOD"][0]), MockContext())
        resp = h_wrap.run_handler()
        del os.environ['AWS_SAM_LOCAL']
        mock_get_handler.assert_called_once()
        mock_metric_publish.assert_not_called()
        print(resp.json())
        self.assertEqual(Status.SUCCESS, resp.status)

    @mock.patch('cfn_resource.handler_wrapper.HandlerWrapper._get_handler', return_value=mock_handler)
    @mock.patch('cfn_resource.metrics.Metrics.publish')
    def test_run_handler_unhandled_exception(self, mock_metric_publish, mock_get_handler):
        mock_get_handler.side_effect = ValueError('blah')
        h_wrap = HandlerWrapper(_get_event(EVENTS["SYNC-GOOD"][0]), MockContext())
        resp = h_wrap.run_handler()
        mock_get_handler.assert_called_once()
        mock_metric_publish.assert_called_once()
        self.assertEqual(Status.FAILED, resp.status)
        self.assertEqual('InternalFailure', resp.errorCode)
        self.assertEqual('ValueError: blah', resp.message)

    @mock.patch('cfn_resource.handler_wrapper.HandlerWrapper._get_handler', return_value=mock_handler)
    @mock.patch('cfn_resource.metrics.Metrics.publish')
    def test_run_handler_handled_exception(self, mock_metric_publish, mock_get_handler):
        # handler fails with exception in cfn_resource.exceptions
        mock_get_handler.side_effect = exceptions.AccessDenied('blah')
        h_wrap = HandlerWrapper(_get_event(EVENTS["SYNC-GOOD"][0]), MockContext())
        resp = h_wrap.run_handler()
        mock_get_handler.assert_called_once()
        mock_metric_publish.assert_called_once()
        self.assertEqual(Status.FAILED, resp.status)
        self.assertEqual('AccessDenied', resp.errorCode)
        self.assertEqual('AccessDenied: blah', resp.message)

    @mock.patch('cfn_resource.handler_wrapper.HandlerWrapper._get_handler', return_value=mock_handler)
    @mock.patch('cfn_resource.metrics.Metrics.publish')
    def test_run_handler_metrics_fail(self, mock_metric_publish, mock_get_handler):
        mock_metric_publish.side_effect = ValueError('blah')
        h_wrap = HandlerWrapper(_get_event(EVENTS["SYNC-GOOD"][0]), MockContext())
        resp = h_wrap.run_handler()
        mock_get_handler.assert_called_once()
        mock_metric_publish.assert_called_once()
        self.assertEqual(Status.FAILED, resp.status)
        self.assertEqual('InternalFailure', resp.errorCode)
        self.assertEqual('ValueError: blah', resp.message)