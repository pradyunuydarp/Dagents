"""PyTorch anomaly model modules and registry."""

from __future__ import annotations

from dataclasses import dataclass

import torch
from torch import Tensor, nn


MODEL_FAMILY_AUTOENCODER = "autoencoder"
MODEL_FAMILY_VARIATIONAL_AUTOENCODER = "variational_autoencoder"
SUPPORTED_MODEL_FAMILIES = {MODEL_FAMILY_AUTOENCODER, MODEL_FAMILY_VARIATIONAL_AUTOENCODER}


@dataclass(frozen=True, slots=True)
class ModelConfig:
    family: str
    hidden_dims: list[int]
    latent_dim: int
    dropout: float = 0.0
    beta: float = 1.0


class BaseAnomalyModule(nn.Module):
    """Base contract for anomaly models."""

    def compute_loss(self, batch: Tensor, target: Tensor | None = None) -> Tensor:
        raise NotImplementedError

    def score_samples(self, batch: Tensor) -> Tensor:
        raise NotImplementedError


class FeedForwardAutoencoder(BaseAnomalyModule):
    def __init__(self, input_dim: int, config: ModelConfig) -> None:
        super().__init__()
        self.config = config
        encoder_dims = [input_dim, *config.hidden_dims, config.latent_dim]
        decoder_dims = [config.latent_dim, *reversed(config.hidden_dims), input_dim]
        self.encoder = _build_mlp(encoder_dims, dropout=config.dropout)
        self.decoder = _build_mlp(decoder_dims, dropout=config.dropout, final_activation=False)

    def forward(self, batch: Tensor) -> Tensor:
        return self.decoder(self.encoder(batch))

    def compute_loss(self, batch: Tensor, target: Tensor | None = None) -> Tensor:
        reconstruction = self.forward(batch)
        expected = batch if target is None else target
        return torch.mean(torch.sum((reconstruction - expected) ** 2, dim=1))

    def score_samples(self, batch: Tensor) -> Tensor:
        reconstruction = self.forward(batch)
        return torch.sum((reconstruction - batch) ** 2, dim=1)


class VariationalAutoencoder(BaseAnomalyModule):
    def __init__(self, input_dim: int, config: ModelConfig) -> None:
        super().__init__()
        self.config = config
        hidden_dims = [input_dim, *config.hidden_dims]
        self.encoder = _build_mlp(hidden_dims, dropout=config.dropout)
        last_hidden = hidden_dims[-1]
        self.mu_layer = nn.Linear(last_hidden, config.latent_dim)
        self.logvar_layer = nn.Linear(last_hidden, config.latent_dim)
        decoder_dims = [config.latent_dim, *reversed(config.hidden_dims), input_dim]
        self.decoder = _build_mlp(decoder_dims, dropout=config.dropout, final_activation=False)

    def forward(self, batch: Tensor) -> tuple[Tensor, Tensor, Tensor]:
        hidden = self.encoder(batch)
        mu = self.mu_layer(hidden)
        logvar = self.logvar_layer(hidden)
        latent = self._reparameterize(mu, logvar)
        reconstruction = self.decoder(latent)
        return reconstruction, mu, logvar

    def compute_loss(self, batch: Tensor, target: Tensor | None = None) -> Tensor:
        reconstruction, mu, logvar = self.forward(batch)
        expected = batch if target is None else target
        recon_loss = torch.sum((reconstruction - expected) ** 2, dim=1)
        kl_loss = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp(), dim=1)
        return torch.mean(recon_loss + self.config.beta * kl_loss)

    def score_samples(self, batch: Tensor) -> Tensor:
        reconstruction, _, _ = self.forward(batch)
        return torch.sum((reconstruction - batch) ** 2, dim=1)

    @staticmethod
    def _reparameterize(mu: Tensor, logvar: Tensor) -> Tensor:
        std = torch.exp(0.5 * logvar)
        epsilon = torch.randn_like(std)
        return mu + epsilon * std


def build_model(input_dim: int, config: ModelConfig) -> BaseAnomalyModule:
    if config.family == MODEL_FAMILY_AUTOENCODER:
        return FeedForwardAutoencoder(input_dim, config)
    if config.family == MODEL_FAMILY_VARIATIONAL_AUTOENCODER:
        return VariationalAutoencoder(input_dim, config)
    raise ValueError(f"Unsupported model family: {config.family}")


def _build_mlp(dimensions: list[int], dropout: float, final_activation: bool = True) -> nn.Sequential:
    layers: list[nn.Module] = []
    for index in range(len(dimensions) - 1):
        in_features = dimensions[index]
        out_features = dimensions[index + 1]
        layers.append(nn.Linear(in_features, out_features))
        is_last = index == len(dimensions) - 2
        if not is_last or final_activation:
            layers.append(nn.ReLU())
            if dropout > 0:
                layers.append(nn.Dropout(dropout))
    return nn.Sequential(*layers)
