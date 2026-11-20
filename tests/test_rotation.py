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


if __name__ == "__main__":
    unittest.main()
