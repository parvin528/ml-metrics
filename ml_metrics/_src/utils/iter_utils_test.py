# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from concurrent import futures
import itertools as it
import queue

from absl.testing import absltest
from absl.testing import parameterized
from ml_metrics._src.utils import iter_utils
import more_itertools as mit
import numpy as np


def mock_range(n, batch_size, batch_fn=lambda x: x):
  """Generates (tuple of) n columns of fake data."""
  for _ in range(1000):
    yield tuple(batch_fn(np.ones(batch_size) * j) for j in range(n))
  raise ValueError(
      'Reached the end of the range, might indicate iterator is first exhausted'
      ' before running.'
  )


class MockIterable:

  def __init__(self, iterable):
    self._iteratable = iterable

  def __len__(self):
    raise NotImplementedError()

  def __iter__(self):
    return iter(self._iteratable)


class UtilsTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.thread_pool = futures.ThreadPoolExecutor()

  def tearDown(self):
    self.thread_pool.shutdown()
    super().tearDown()

  def test_iterator_pipe_normal(self):

    def inc_(input_iter):
      return map(lambda x: x + 1, input_iter)

    iter_pipe = iter_utils.IteratorPipe.new(inc_, timeout=1)
    self.assertIsNotNone(iter_pipe.input_queue)
    self.assertIsNotNone(iter_pipe.output_queue)
    iter_pipe = iter_pipe.submit_to(self.thread_pool)
    mit.last(iter_utils.enqueue_from_iterator(range(10), iter_pipe.input_queue))
    self.assertEqual(10, iter_pipe.state.result())
    actual = list(iter_utils.dequeue_as_iterator(iter_pipe.output_queue))
    self.assertEqual(list(range(1, 11)), actual)

  def test_iterator_pipe_source(self):

    iter_pipe = iter_utils.IteratorPipe.new(
        range(10), input_qsize=None, timeout=1
    ).submit_to(self.thread_pool)
    self.assertEqual(9, iter_pipe.state.result())
    actual = list(iter_utils.dequeue_as_iterator(iter_pipe.output_queue))
    self.assertEqual(list(range(10)), actual)
    self.assertIsNone(iter_pipe.input_queue)

  def test_iterator_pipe_sink(self):

    def consumer(iterator):
      for x in iterator:
        del x

    iter_pipe = iter_utils.IteratorPipe.new(
        consumer, output_qsize=None, timeout=1
    )
    self.assertIsNone(iter_pipe.output_queue)
    self.assertIsNotNone(iter_pipe.input_queue)
    iter_pipe = iter_pipe.submit_to(self.thread_pool)
    mit.last(iter_utils.enqueue_from_iterator(range(10), iter_pipe.input_queue))
    self.assertIsNone(iter_pipe.state.result())
    # Nothing dequeued from output.
    self.assertEqual(10, iter_pipe.progress.processed_cnt)

  def test_iterator_pipe_timeout(self):

    def inc_(input_iter):
      return map(lambda x: x + 1, input_iter)

    iter_pipe = iter_utils.IteratorPipe.new(inc_, timeout=1).submit_to(
        self.thread_pool
    )
    self.assertIsNotNone(iter_pipe.output_queue)
    self.assertIsNotNone(iter_pipe.input_queue)
    iter_pipe.input_queue.put(0)
    with self.assertRaisesRegex(TimeoutError, '(De|En)queue timeout after'):
      iter_pipe._state.result()

  def test_enqueue_dequeue_from_generator(self):
    q = queue.Queue()
    expected = list(iter_utils.enqueue_from_iterator(range(10), q))
    actual = list(iter_utils.dequeue_as_iterator(q))
    self.assertSequenceEqual(expected, actual)

  def test_enqueue_from_generator_timeout(self):
    q = queue.Queue(maxsize=1)
    with self.assertRaisesRegex(TimeoutError, 'Enqueue timeout after'):
      list(iter_utils.enqueue_from_iterator(range(2), q, timeout=0.1))

  def test_dequeue_from_generator_timeout(self):
    q = queue.Queue(maxsize=1)
    q.put(1)
    with self.assertRaisesRegex(TimeoutError, 'Dequeue timeout after'):
      list(iter_utils.dequeue_as_iterator(q, timeout=0.1))

  def test_prefetched_iterator(self):
    iterator = iter_utils.PrefetchedIterator(range(10), prefetch_size=2)
    iterator.prefetch()
    self.assertEqual(2, iterator.cnt)
    self.assertEqual([0, 1], iterator.flush_prefetched())
    self.assertEqual(list(range(2, 10)), list(iterator))

  @parameterized.named_parameters([
      dict(
          testcase_name='no_op',
          expected=[
              (np.zeros(2), np.ones(2)),
              (np.zeros(2), np.ones(2)),
              (np.zeros(2), np.ones(2)),
              (np.zeros(2), np.ones(2)),
              (np.zeros(2), np.ones(2)),
          ],
      ),
      dict(
          testcase_name='to_larger_batch',
          batch_size=3,
          expected=[
              (np.zeros(3), np.ones(3)),
              (np.zeros(3), np.ones(3)),
              (np.zeros(3), np.ones(3)),
              (np.zeros(1), np.ones(1)),
          ],
      ),
      dict(
          testcase_name='batch_size_is_same',
          batch_size=2,
          expected=[
              (np.zeros(2), np.ones(2)),
          ]
          * 5,
      ),
      dict(
          testcase_name='to_smaller_batch',
          batch_size=1,
          expected=[
              (np.zeros(1), np.ones(1)),
          ]
          * 10,
      ),
      dict(
          testcase_name='with_list',
          batch_size=4,
          batch_fn=list,
          expected=[
              ([0, 0, 0, 0], [1, 1, 1, 1]),
              ([0, 0, 0, 0], [1, 1, 1, 1]),
              ([0, 0], [1, 1]),
          ],
      ),
      dict(
          testcase_name='with_tuple',
          batch_size=4,
          batch_fn=tuple,
          expected=[
              ((0, 0, 0, 0), (1, 1, 1, 1)),
              ((0, 0, 0, 0), (1, 1, 1, 1)),
              ((0, 0), (1, 1)),
          ],
      ),
  ])
  def test_rebatched(self, expected, batch_size=0, batch_fn=lambda x: x):
    inputs = it.islice(mock_range(2, batch_size=2, batch_fn=batch_fn), 5)
    actual = iter_utils.rebatched_tuples(
        inputs, batch_size=batch_size, num_columns=2
    )
    for a, b in zip(expected, actual, strict=True):
      np.testing.assert_array_almost_equal(a, b)

  def test_batch_non_sequence_type(self):
    inputs = [(1, 2), (3, 4)]
    with self.assertRaisesRegex(TypeError, 'Non sequence type'):
      next(
          iter_utils.rebatched_tuples(iter(inputs), batch_size=4, num_columns=2)
      )

  def test_batch_unsupported_type(self):
    inputs = [('aaa', 'bbb'), ('aaa', 'bbb')]
    with self.assertRaisesRegex(TypeError, 'Unsupported container type'):
      next(
          iter_utils.rebatched_tuples(iter(inputs), batch_size=4, num_columns=2)
      )

  def test_recitable_iterator_normal(self):
    inputs = range(3)
    it_inputs = iter_utils._RecitableIterator(inputs)
    it_outputs = map(lambda x: x + 1, it_inputs)
    actual = list(zip(it_outputs, it_inputs.recite_iterator(), strict=True))
    self.assertEqual([(1, 0), (2, 1), (3, 2)], actual)

  @parameterized.named_parameters([
      dict(
          testcase_name='to_larger_batch',
          input_batch_size=2,
          fn_batch_size=5,
          num_columns=2,
          num_batches=30,
      ),
      dict(
          testcase_name='to_smaller_batch',
          input_batch_size=5,
          fn_batch_size=2,
          num_columns=2,
          num_batches=30,
      ),
      dict(
          testcase_name='to_same_batch_size',
          input_batch_size=3,
          fn_batch_size=3,
          num_columns=2,
          num_batches=30,
      ),
      dict(
          testcase_name='to_one_element_batch',
          input_batch_size=2,
          fn_batch_size=1,
          num_columns=2,
          num_batches=30,
      ),
      dict(
          testcase_name='from_one_element_batch',
          input_batch_size=1,
          fn_batch_size=5,
          num_columns=2,
          num_batches=30,
      ),
  ])
  def test_recitable_iterator_with_rebatch(
      self,
      input_batch_size=2,
      fn_batch_size=3,
      num_columns=2,
      num_batches=5,
  ):

    inputs = it.islice(
        mock_range(num_columns, batch_size=input_batch_size), num_batches
    )

    def foo(columns):
      assert len(columns[0]) <= fn_batch_size, f'got {columns=}.'
      return tuple(np.array(column) + 1 for column in columns)

    def process_generator(it_inputs):
      it_fn_inputs = iter_utils.rebatched_tuples(
          it_inputs, batch_size=fn_batch_size, num_columns=num_columns
      )
      yield from iter_utils.rebatched_tuples(
          map(foo, it_fn_inputs),
          batch_size=input_batch_size,
          num_columns=num_columns,
      )

    # Setting a max buffer size to make sure the buffer is flushed while
    # iterating.
    actual = iter_utils.processed_with_inputs(process_generator, inputs)
    outputs, original = zip(*actual)

    expected_orignal = list(
        it.islice(
            mock_range(num_columns, batch_size=input_batch_size), num_batches
        )
    )
    expected_outputs = [np.array(x) + 1 for x in expected_orignal]
    np.testing.assert_array_equal(expected_orignal, original)
    np.testing.assert_array_equal(expected_outputs, outputs)


if __name__ == '__main__':
  absltest.main()