import unittest
from rotation import ShuffledRotation


class ShuffledRotationTests(unittest.TestCase):
    def test_each_camera_appears_once_per_random_cycle(self):
        cameras = [{"url": str(i)} for i in range(20)]
        rotation = ShuffledRotation()
        previous_last = None
        for _ in range(20):
            cycle = [rotation.next(cameras, {})[0]["url"] for _ in cameras]
            self.assertEqual(set(cycle), {cam["url"] for cam in cameras})
            self.assertEqual(len(cycle), len(set(cycle)))
            if previous_last is not None:
                self.assertNotEqual(cycle[0], previous_last)
            previous_last = cycle[-1]

    def test_cooldown_and_new_catalog_are_respected(self):
        cameras = [{"url": "a"}, {"url": "b"}]
        rotation = ShuffledRotation()
        self.assertEqual(rotation.next(cameras, {"a": float("inf")})[0]["url"], "b")
        updated = [{"url": "c"}]
        self.assertEqual(rotation.next(updated, {})[0]["url"], "c")
        self.assertEqual(rotation.next(updated, {"c": float("inf")}), (None, None))

    def test_prev_and_next_navigation(self):
        cameras = [{"url": "1"}, {"url": "2"}, {"url": "3"}]
        rotation = ShuffledRotation()
        # prev on empty history returns (None, None)
        self.assertEqual(rotation.prev(), (None, None))

        cam1, _ = rotation.next(cameras, {})
        cam2, _ = rotation.next(cameras, {})
        cam3, _ = rotation.next(cameras, {})

        # We are at cam3. Calling prev should give cam2
        prev_cam, next_cam = rotation.prev()
        self.assertEqual(prev_cam["url"], cam2["url"])
        self.assertEqual(next_cam["url"], cam3["url"])

        # Calling prev again should give cam1
        prev_cam2, next_cam2 = rotation.prev()
        self.assertEqual(prev_cam2["url"], cam1["url"])
        self.assertEqual(next_cam2["url"], cam2["url"])

        # Calling prev at start should still stay at cam1
        prev_cam3, _ = rotation.prev()
        self.assertEqual(prev_cam3["url"], cam1["url"])

        # Calling next should traverse forward: cam2
        fwd_cam, _ = rotation.next(cameras, {})
        self.assertEqual(fwd_cam["url"], cam2["url"])

        # Calling next should reach cam3
        fwd_cam2, _ = rotation.next(cameras, {})
        self.assertEqual(fwd_cam2["url"], cam3["url"])



if __name__ == "__main__":
    unittest.main()
