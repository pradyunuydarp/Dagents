"""Tests for the model inventory's provider adapters.

These cover the part of the abstraction that is easy to get wrong and expensive
to get wrong late: **provisioning**. A model either is here or it is not, and the
adapter has to say which, in a way a caller can act on, without ever quietly
reaching for the network.

So the Hugging Face tests assert the refusals rather than a successful download.
Asserting a real Hub fetch would make this suite need the network, which would
make it a suite that gets skipped — and this repo has already learned what a
skipped test is worth.

The PyTorch tests do go end to end, because they can: training a tiny
autoencoder, saving the artifact, and loading it back through the adapter needs
no network at all — and that round trip is new behaviour, since nothing in the
service used to read an artifact back for inference.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import unittest.mock

import numpy as np
import pandas as pd

from app.ml.inventory import (
    ANOMALY_DETECTION,
    CLASSIFICATION,
    EMBEDDING,
    FORECASTING,
    CapabilityStatus,
    MissingModelDependencyError,
    ModelCapability,
    ModelDownloadNotPermittedError,
    ModelProvider,
    UnsupportedModelError,
    build_default_inventory,
    load_adapter,
)
from app.ml.providers import huggingface_provider, torch_provider


def transformers_installed() -> bool:
    """Whether the optional Hugging Face stack is available in this environment."""
    try:
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


class LoadAdapterTests(unittest.TestCase):
    """The factory resolves entrypoints lazily and refuses what it cannot build."""

    def setUp(self) -> None:
        self.inventory = build_default_inventory()

    #: Constructor arguments a capability's adapter cannot default. Everything
    #: else builds from the capability alone.
    REQUIRED_OPTIONS = {FORECASTING: {"input_dim": 3}}

    def test_every_implemented_capability_builds_an_unloaded_adapter(self) -> None:
        """A dotted entrypoint is a string; nothing checks it until it is imported.

        Building each one proves more than `hasattr` would: that the module
        imports, that the attribute is callable, and that it accepts a
        capability — so a typo or a signature change is caught here rather than
        by the first request that needed that family, in whatever environment
        that happened to be.
        """
        built = 0
        for capability in self.inventory.capabilities():
            if capability.status is not CapabilityStatus.IMPLEMENTED:
                continue
            with self.subTest(family=capability.family, task=capability.task):
                adapter = load_adapter(
                    capability, **self.REQUIRED_OPTIONS.get(capability.task, {})
                )
                self.assertIs(capability, adapter.capability)
                self.assertFalse(
                    adapter.is_loaded,
                    "constructing an adapter must not load anything",
                )
                built += 1
        self.assertGreater(built, 0, "No implemented capabilities were exercised")

    def test_builds_a_torch_adapter_for_an_anomaly_family(self) -> None:
        adapter = load_adapter(self.inventory.resolve("autoencoder", ANOMALY_DETECTION))
        self.assertIsInstance(adapter, torch_provider.TorchAnomalyAdapter)
        self.assertFalse(adapter.is_loaded)

    def test_builds_a_hugging_face_adapter_for_the_embedding_family(self) -> None:
        adapter = load_adapter(self.inventory.resolve("transformer", EMBEDDING))
        self.assertIsInstance(adapter, huggingface_provider.TransformerEmbeddingAdapter)
        self.assertFalse(adapter.is_loaded)

    def test_building_an_adapter_loads_nothing(self) -> None:
        """Construction is configuration. Weights arrive only on `load`."""
        for family, task in (("autoencoder", ANOMALY_DETECTION), ("transformer", EMBEDDING)):
            with self.subTest(family=family):
                adapter = load_adapter(self.inventory.resolve(family, task))
                self.assertFalse(adapter.is_loaded)

    def test_an_unimportable_entrypoint_names_the_requirements(self) -> None:
        capability = ModelCapability(
            family="linear",
            task=CLASSIFICATION,
            provider=ModelProvider.SCIKIT_LEARN,
            status=CapabilityStatus.IMPLEMENTED,
            entrypoint="app.ml.providers.not_a_module:build",
            extra_requirements=("some-package>=1",),
            notes="",
        )
        with self.assertRaises(MissingModelDependencyError) as caught:
            load_adapter(capability)
        self.assertIn("some-package>=1", str(caught.exception))

    def test_a_malformed_entrypoint_is_rejected(self) -> None:
        capability = ModelCapability(
            family="linear",
            task=CLASSIFICATION,
            provider=ModelProvider.SCIKIT_LEARN,
            status=CapabilityStatus.IMPLEMENTED,
            entrypoint="app.ml.checks",  # no `:attribute`
            notes="",
        )
        with self.assertRaises(ValueError):
            load_adapter(capability)


class HuggingFaceProvisioningTests(unittest.TestCase):
    """What the Hugging Face adapter does when the model is not here."""

    def setUp(self) -> None:
        self.capability = build_default_inventory().resolve("transformer", EMBEDDING)

    def test_default_download_policy_is_refuse(self) -> None:
        """The safe default. A fetch has to be asked for."""
        adapter = huggingface_provider.TransformerEmbeddingAdapter(self.capability)
        self.assertFalse(adapter.allow_download)

    def test_the_environment_switch_can_permit_downloads(self) -> None:
        with unittest.mock.patch.dict(
            "os.environ", {huggingface_provider.ALLOW_DOWNLOAD_ENV: "1"}
        ):
            adapter = huggingface_provider.TransformerEmbeddingAdapter(self.capability)
        self.assertTrue(adapter.allow_download)

    def test_an_explicit_argument_beats_the_environment_switch(self) -> None:
        with unittest.mock.patch.dict(
            "os.environ", {huggingface_provider.ALLOW_DOWNLOAD_ENV: "1"}
        ):
            adapter = huggingface_provider.TransformerEmbeddingAdapter(
                self.capability, allow_download=False
            )
        self.assertFalse(adapter.allow_download)

    def test_it_uses_the_capabilitys_checkpoint_by_default(self) -> None:
        adapter = huggingface_provider.TransformerEmbeddingAdapter(self.capability)
        self.assertEqual(self.capability.default_checkpoint, adapter.checkpoint)

    def test_a_capability_with_no_checkpoint_cannot_be_constructed(self) -> None:
        bare = ModelCapability(
            family="transformer",
            task=EMBEDDING,
            provider=ModelProvider.HUGGINGFACE,
            status=CapabilityStatus.IMPLEMENTED,
            entrypoint="app.ml.providers.huggingface_provider:build_embedding_adapter",
            notes="",
        )
        with self.assertRaises(ValueError):
            huggingface_provider.TransformerEmbeddingAdapter(bare)

    def test_load_without_the_hub_stack_names_what_to_install(self) -> None:
        """The message has to be actionable in the environment that hit it."""
        adapter = huggingface_provider.TransformerEmbeddingAdapter(self.capability)
        with unittest.mock.patch.object(
            huggingface_provider, "_import_backends", side_effect=ImportError("no transformers")
        ):
            with self.assertRaises(MissingModelDependencyError) as caught:
                adapter.load()
        message = str(caught.exception)
        self.assertIn("transformers", message)
        self.assertIn("requirements-optional-models.txt", message)

    def test_a_cache_miss_becomes_a_typed_refusal_not_a_hub_error(self) -> None:
        """Our translation of `local_files_only`, tested without the Hub stack.

        The behaviour under test is this module's: turn "the weights are not
        here" into something a caller can distinguish from "the model is
        broken", and say how to fix it.
        """
        adapter = huggingface_provider.TransformerEmbeddingAdapter(self.capability)

        class MissingFromCache:
            @staticmethod
            def from_pretrained(*args: object, **kwargs: object):
                raise OSError("not found in the local cache")

        with unittest.mock.patch.object(
            huggingface_provider,
            "_import_backends",
            return_value=(object(), MissingFromCache, MissingFromCache),
        ):
            with self.assertRaises(ModelDownloadNotPermittedError) as caught:
                adapter.load()
        message = str(caught.exception)
        self.assertIn(self.capability.default_checkpoint, message)
        self.assertIn(huggingface_provider.ALLOW_DOWNLOAD_ENV, message)

    def test_a_load_failure_is_re_raised_when_downloads_were_permitted(self) -> None:
        """With downloads allowed, an OSError is a real failure, not a policy refusal."""
        adapter = huggingface_provider.TransformerEmbeddingAdapter(
            self.capability, allow_download=True
        )

        class Broken:
            @staticmethod
            def from_pretrained(*args: object, **kwargs: object):
                raise OSError("the repository is corrupt")

        with unittest.mock.patch.object(
            huggingface_provider, "_import_backends", return_value=(object(), Broken, Broken)
        ):
            with self.assertRaises(OSError) as caught:
                adapter.load()
        self.assertNotIsInstance(caught.exception, ModelDownloadNotPermittedError)

    def test_embedding_before_loading_is_an_error(self) -> None:
        adapter = huggingface_provider.TransformerEmbeddingAdapter(self.capability)
        with self.assertRaises(RuntimeError):
            adapter.embed(["hello"])

    @unittest.skipUnless(
        transformers_installed(),
        "transformers is not installed; install services/model-service/"
        "requirements-optional-models.txt to exercise the real Hub stack",
    )
    def test_the_real_hub_stack_refuses_an_uncached_checkpoint(self) -> None:
        """Validates the assumption the test above is built on.

        The faked test proves our translation. This proves `transformers` really
        raises `OSError` for a cache miss under `local_files_only`, so the
        translation is wired to the right exception. A fresh `cache_dir`
        guarantees the miss regardless of what the developer's own Hub cache
        happens to hold.
        """
        with tempfile.TemporaryDirectory() as empty_cache:
            adapter = huggingface_provider.TransformerEmbeddingAdapter(
                self.capability, allow_download=False, cache_dir=empty_cache
            )
            with self.assertRaises(ModelDownloadNotPermittedError):
                adapter.load()


class TorchAnomalyRoundTripTests(unittest.TestCase):
    """Train, save, load back, score — the inference path the service lacked."""

    @classmethod
    def setUpClass(cls) -> None:
        from app.ml.pipeline import PipelineConfig, UnifiedAnomalyTrainingPipeline

        rng = np.random.default_rng(7)
        normal = rng.normal(loc=0.0, scale=1.0, size=(80, 4))
        anomalous = rng.normal(loc=6.0, scale=1.0, size=(20, 4))
        cls.features = pd.DataFrame(
            np.vstack([normal, anomalous]), columns=["a", "b", "c", "d"]
        ).astype("float32")
        cls.labels = np.concatenate([np.zeros(80, dtype=np.int64), np.ones(20, dtype=np.int64)])

        cls._directory = tempfile.TemporaryDirectory()
        cls.artifact_path = Path(cls._directory.name) / "roundtrip-autoencoder.pt"
        pipeline = UnifiedAnomalyTrainingPipeline(
            PipelineConfig(
                dataset_name="roundtrip",
                model_family="autoencoder",
                test_size=0.2,
                tuning_strategy="holdout",
                n_splits=2,
                leave_one_out_max_samples=16,
                target_metric="average_precision",
                random_seed=7,
                device="cpu",
            )
        )
        # A single tiny candidate keeps this a round-trip test rather than a
        # hyperparameter search that happens to take a minute.
        cls.result = pipeline.train(
            cls.features,
            cls.labels,
            search_space={
                "hidden_dims": [[8]],
                "latent_dim": [3],
                "dropout": [0.0],
                "learning_rate": [0.01],
                "batch_size": [16],
                "epochs": [4],
                "patience": [2],
                "pca_components": [None],
            },
            artifact_path=cls.artifact_path,
            use_pca=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    def adapter(self) -> torch_provider.TorchAnomalyAdapter:
        capability = build_default_inventory().resolve("autoencoder", ANOMALY_DETECTION)
        return load_adapter(capability, artifact_path=self.artifact_path)

    def test_the_artifact_was_written(self) -> None:
        self.assertTrue(self.artifact_path.is_file())

    def test_loading_restores_the_trained_threshold(self) -> None:
        adapter = self.adapter()
        self.assertIsNone(adapter.threshold)
        adapter.load()
        self.assertTrue(adapter.is_loaded)
        self.assertEqual(self.result.metrics.threshold, adapter.threshold)

    def test_scoring_returns_one_finite_score_per_row(self) -> None:
        adapter = self.adapter()
        adapter.load()
        scores = adapter.score(self.features.to_numpy(dtype=np.float32))
        self.assertEqual((len(self.features),), scores.shape)
        self.assertTrue(np.all(np.isfinite(scores)))

    def test_reloading_the_artifact_scores_identically(self) -> None:
        """The artifact has to carry everything inference needs.

        If the saved preprocessing or hyperparameters were incomplete, two loads
        of the same file would disagree — and the disagreement would show up as
        drifting anomaly scores in production, not as an error.
        """
        rows = self.features.to_numpy(dtype=np.float32)
        first = self.adapter()
        first.load()
        second = self.adapter()
        second.load()
        np.testing.assert_allclose(first.score(rows), second.score(rows))

    def test_the_anomalous_rows_score_higher_than_the_normal_ones(self) -> None:
        """A round trip that returned numbers but lost the model would pass the
        shape assertions above. This one would not."""
        adapter = self.adapter()
        adapter.load()
        scores = adapter.score(self.features.to_numpy(dtype=np.float32))
        self.assertGreater(float(scores[80:].mean()), float(scores[:80].mean()))

    def test_flagging_uses_the_artifacts_own_threshold(self) -> None:
        adapter = self.adapter()
        adapter.load()
        rows = self.features.to_numpy(dtype=np.float32)
        np.testing.assert_array_equal(
            adapter.score(rows) >= adapter.threshold, adapter.flag(rows)
        )

    def test_scoring_before_loading_is_an_error(self) -> None:
        with self.assertRaises(RuntimeError):
            self.adapter().score(self.features.to_numpy(dtype=np.float32))

    def test_a_family_mismatch_is_refused(self) -> None:
        """Loading an autoencoder artifact as a VAE would score with the wrong model.

        It would not crash — both expose `score_samples` — so nothing but this
        check stands between a mismatched artifact and numbers reported as
        though they were right.
        """
        capability = build_default_inventory().resolve(
            "variational_autoencoder", ANOMALY_DETECTION
        )
        adapter = load_adapter(capability, artifact_path=self.artifact_path)
        with self.assertRaises(UnsupportedModelError):
            adapter.load()

    def test_a_missing_artifact_is_refused(self) -> None:
        capability = build_default_inventory().resolve("autoencoder", ANOMALY_DETECTION)
        adapter = load_adapter(capability, artifact_path=self.artifact_path.with_name("absent.pt"))
        with self.assertRaises(ValueError):
            adapter.load()

    def test_an_adapter_with_no_artifact_path_is_refused(self) -> None:
        capability = build_default_inventory().resolve("autoencoder", ANOMALY_DETECTION)
        with self.assertRaises(ValueError):
            load_adapter(capability).load()


class TorchForecastingAdapterTests(unittest.TestCase):
    """The recurrent families, behind the same provisioning contract."""

    def test_each_family_builds_its_own_recurrent_cell(self) -> None:
        from torch import nn

        inventory = build_default_inventory()
        for family, expected in (("gru", nn.GRU), ("lstm", nn.LSTM)):
            with self.subTest(family=family):
                adapter = load_adapter(
                    inventory.resolve(family, FORECASTING), input_dim=3, hidden_dim=8
                )
                adapter.load()
                self.assertIsInstance(adapter._model.recurrent, expected)

    def test_prediction_returns_one_value_per_sequence(self) -> None:
        inventory = build_default_inventory()
        adapter = load_adapter(
            inventory.resolve("gru", FORECASTING), input_dim=3, hidden_dim=8
        )
        adapter.load()
        sequences = np.zeros((5, 4, 3), dtype=np.float32)
        self.assertEqual((5,), adapter.predict(sequences).shape)

    def test_predicting_before_loading_is_an_error(self) -> None:
        adapter = load_adapter(
            build_default_inventory().resolve("lstm", FORECASTING), input_dim=2
        )
        with self.assertRaises(RuntimeError):
            adapter.predict(np.zeros((1, 2, 2), dtype=np.float32))


if __name__ == "__main__":
    unittest.main()
