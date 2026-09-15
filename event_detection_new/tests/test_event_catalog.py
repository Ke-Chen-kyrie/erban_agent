import unittest

from prompt import DETAILED_SUMMARY_PROMPT, EVENT_LABELS, build_detect_prompt


class DetectionEventCatalogTest(unittest.TestCase):
    def test_rub_leg_is_not_an_available_detection_event(self) -> None:
        detection_prompt = build_detect_prompt()

        self.assertNotIn("rub_leg", EVENT_LABELS)
        self.assertNotIn("rub_leg", detection_prompt)
        self.assertNotIn("揉腿", detection_prompt)
        self.assertNotIn("rub_leg", DETAILED_SUMMARY_PROMPT)

    def test_indoor_smoking_has_been_removed(self) -> None:
        detection_prompt = build_detect_prompt()

        self.assertNotIn("indoor_smoking", EVENT_LABELS)
        self.assertNotIn("indoor_smoking", detection_prompt)
        self.assertNotIn("室内吸烟", detection_prompt)
        self.assertNotIn("indoor_smoking", DETAILED_SUMMARY_PROMPT)

    def test_prompt_instructs_model_to_report_person_name(self) -> None:
        detection_prompt = build_detect_prompt()

        self.assertIn("身份标注说明", detection_prompt)
        self.assertIn('"name"', detection_prompt)
        self.assertIn("张三", detection_prompt)
        self.assertIn("路人", detection_prompt)


if __name__ == "__main__":
    unittest.main()
