"""Tests for classification and regression checks."""

from __future__ import annotations

import math
import unittest

from app.models import MLCheckRequest
from app.services.training_service import ModelTrainingService


class MLCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ModelTrainingService()
        self.records = [
            {"f1": 0.0, "f2": 1.0, "label": 0, "target": 1.0},
            {"f1": 0.2, "f2": 0.8, "label": 0, "target": 1.3},
            {"f1": 0.8, "f2": 0.2, "label": 1, "target": 2.5},
            {"f1": 1.0, "f2": 0.0, "label": 1, "target": 2.9},
            {"f1": 0.1, "f2": 0.9, "label": 0, "target": 1.1},
            {"f1": 0.9, "f2": 0.1, "label": 1, "target": 2.7},
        ]

    def test_classification_check_with_inline_records(self) -> None:
        response = self.service.classification_check(
            MLCheckRequest(
                dataset={"inline_records": self.records},
                feature_fields=["f1", "f2"],
                label_field="label",
                model_family="random_forest",
                max_rows=100,
            )
        )
        self.assertEqual(response.dataset_name, "source-backed")
        self.assertEqual(response.label_field, "label")
        self.assertGreaterEqual(response.metrics.accuracy, 0.5)

    def test_regression_check_with_inline_records(self) -> None:
        response = self.service.regression_check(
            MLCheckRequest(
                dataset={"inline_records": self.records},
                feature_fields=["f1", "f2"],
                label_field="target",
                model_family="linear",
                max_rows=100,
            )
        )
        self.assertEqual(response.dataset_name, "source-backed")
        self.assertEqual(response.label_field, "target")
        self.assertLess(response.metrics.rmse, 1.0)

    def test_forecasting_checks_with_inline_records(self) -> None:
        records = [{"amount": math.sin(index / 4.0), "target": math.sin(index / 4.0)} for index in range(1, 80)]

        gru = self.service.forecasting_check(
            MLCheckRequest(
                dataset={"inline_records": records},
                feature_fields=["amount"],
                label_field="target",
                model_family="gru",
                max_rows=100,
                sequence_length=6,
            )
        )
        lstm = self.service.forecasting_check(
            MLCheckRequest(
                dataset={"inline_records": records},
                feature_fields=["amount"],
                label_field="target",
                model_family="lstm",
                max_rows=100,
                sequence_length=6,
            )
        )

        self.assertEqual(gru.dataset_name, "source-backed")
        self.assertEqual(lstm.dataset_name, "source-backed")
        self.assertLess(gru.metrics.rmse, 1.0)
        self.assertLess(lstm.metrics.rmse, 1.0)


if __name__ == "__main__":
    unittest.main()
