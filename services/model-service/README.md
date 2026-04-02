# Dagents Model Service

Project-generic anomaly-model training and inference service for Dagents consumers such as Watchdog, Datalytics, and future agents.

## Purpose

This service owns reusable ML pipeline concerns that should not be hardcoded into individual product services:

- benchmark dataset loading
- preprocessing and PCA
- unified train/validation/test orchestration
- cross-validation and leave-one-out evaluation modes
- PyTorch model training
- hyperparameter search
- artifact persistence

## Current Focus

The first implemented family is tabular anomaly detection using reconstruction-based PyTorch models:

- `autoencoder`
- `variational_autoencoder`

## Public API

- `GET /api/v1/health`
- `GET /api/v1/datasets`
- `POST /api/v1/train`

## Supported Benchmark Datasets

- `kddcup99_http`
- `creditcard_openml`
- `mammography_openml`

## Local Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## CLI Training

```bash
python -m app.ml.train
```
