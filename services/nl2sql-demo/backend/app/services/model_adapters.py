"""NL2SQL model adapters for artifacts produced by the notebooks."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.models import ModelDescriptor, SchemaTable
from app.services.schema import ddl_from_tables, normalize_identifier


CODEQWEN_SYSTEM_PROMPT = (
    "You are a SQL expert. Strictly output ONLY the SQL query requested. "
    "Do NOT include additional columns, filters, or JOINs not explicitly required by the question."
)


class NL2SQLAdapter(Protocol):
    descriptor: ModelDescriptor

    def generate(self, question: str, context: str, max_new_tokens: int) -> str:
        ...

    def prompt(self, question: str, context: str) -> str:
        ...


@dataclass
class HeuristicAdapter:
    descriptor: ModelDescriptor
    tables: list[SchemaTable]

    def prompt(self, question: str, context: str) -> str:
        return f"question: {question} context: {context}"

    def generate(self, question: str, context: str, max_new_tokens: int) -> str:
        del context, max_new_tokens
        return heuristic_sql(question, self.tables)


class CodeT5Seq2SeqAdapter:
    def __init__(self, descriptor: ModelDescriptor, model_path: Path) -> None:
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        import torch

        self.descriptor = descriptor
        self.torch = torch
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=False)
        dtype = torch.float16 if torch.cuda.is_available() else torch.float32
        self.model = AutoModelForSeq2SeqLM.from_pretrained(model_path, torch_dtype=dtype).to(self.device)
        self.model.eval()

    def prompt(self, question: str, context: str) -> str:
        return f"question: {question} context: {context}"

    def generate(self, question: str, context: str, max_new_tokens: int) -> str:
        text = self.prompt(question, context)
        inputs = self.tokenizer(text, return_tensors="pt", max_length=512, truncation=True).to(self.device)
        with self.torch.no_grad():
            outputs = self.model.generate(
                inputs.input_ids,
                attention_mask=inputs.attention_mask,
                max_length=max_new_tokens,
                num_beams=4,
                early_stopping=True,
            )
        return self.tokenizer.decode(outputs[0], skip_special_tokens=True).strip()


class CodeQwenLoraAdapter:
    def __init__(self, descriptor: ModelDescriptor, adapter_path: Path) -> None:
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch

        self.descriptor = descriptor
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained("Qwen/CodeQwen1.5-7B-Chat", trust_remote_code=True)
        self.tokenizer.pad_token = self.tokenizer.eos_token
        base_model = AutoModelForCausalLM.from_pretrained(
            "Qwen/CodeQwen1.5-7B-Chat",
            device_map="auto",
            torch_dtype=torch.float16,
            trust_remote_code=True,
        )
        self.model = PeftModel.from_pretrained(base_model, adapter_path)
        self.model.eval()
        self.eos_token_id = self.tokenizer.convert_tokens_to_ids("<|im_end|>")

    def prompt(self, question: str, context: str) -> str:
        messages = [
            {"role": "system", "content": CODEQWEN_SYSTEM_PROMPT},
            {"role": "user", "content": f"Context: {context}\n\nQuestion: {question}"},
        ]
        return self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)

    def generate(self, question: str, context: str, max_new_tokens: int) -> str:
        prompt = self.prompt(question, context)
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        with self.torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                eos_token_id=self.eos_token_id,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        generated_ids = outputs[0][inputs["input_ids"].shape[1] :]
        return self.tokenizer.decode(generated_ids, skip_special_tokens=True).strip()


class NL2SQLModelRegistry:
    """Discovers zipped notebook artifacts and lazily loads selected adapters."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._loaded: dict[str, NL2SQLAdapter] = {}

    def descriptors(self) -> list[ModelDescriptor]:
        artifact_descriptors = [self._descriptor_for_zip(path) for path in sorted(self.settings.models_dir.glob("*.zip"))]
        default_model_id = self.settings.default_model_id
        artifact_descriptors.sort(key=lambda descriptor: (descriptor.model_id != default_model_id, descriptor.model_id))
        fallback_descriptor = ModelDescriptor(
            model_id="heuristic_demo",
            label="Dagents heuristic fallback",
            adapter_kind="heuristic",
            prompt_format="question: {question} context: {ddl}",
            notes="Fast deterministic fallback for presentations without GPU/model dependencies.",
        )
        descriptors = [*artifact_descriptors, fallback_descriptor]
        return descriptors

    def descriptor(self, model_id: str | None) -> ModelDescriptor:
        selected = model_id or self.settings.default_model_id
        for descriptor in self.descriptors():
            if descriptor.model_id == selected:
                return descriptor
        return self.descriptors()[0]

    def prompt_for(self, descriptor: ModelDescriptor, question: str, context: str) -> str:
        if descriptor.adapter_kind == "codeqwen_lora":
            return (
                "<|im_start|>system\n"
                f"{CODEQWEN_SYSTEM_PROMPT}<|im_end|>\n"
                "<|im_start|>user\n"
                f"Context: {context}\n\nQuestion: {question}<|im_end|>\n"
                "<|im_start|>assistant\n"
            )
        return f"question: {question} context: {context}"

    def generate(self, descriptor: ModelDescriptor, question: str, tables: list[SchemaTable], max_new_tokens: int) -> tuple[str, str, bool]:
        context = ddl_from_tables(tables)
        prompt = self.prompt_for(descriptor, question, context)
        if descriptor.adapter_kind == "heuristic":
            return heuristic_sql(question, tables), prompt, True
        try:
            adapter = self._load(descriptor, tables)
            return adapter.generate(question, context, max_new_tokens), adapter.prompt(question, context), False
        except Exception:
            if not self.settings.allow_fallback:
                raise
            return heuristic_sql(question, tables), prompt, True

    def _load(self, descriptor: ModelDescriptor, tables: list[SchemaTable]) -> NL2SQLAdapter:
        if descriptor.model_id in self._loaded:
            return self._loaded[descriptor.model_id]
        if descriptor.adapter_kind == "heuristic":
            adapter: NL2SQLAdapter = HeuristicAdapter(descriptor, tables)
        else:
            artifact_path = self._extract(descriptor)
            model_path = self._model_root(artifact_path)
            if descriptor.adapter_kind == "codeqwen_lora":
                adapter = CodeQwenLoraAdapter(descriptor, model_path)
            else:
                adapter = CodeT5Seq2SeqAdapter(descriptor, model_path)
        self._loaded[descriptor.model_id] = adapter
        return adapter

    def _descriptor_for_zip(self, path: Path) -> ModelDescriptor:
        stem = path.stem.replace("(", "").replace(")", "").replace(" ", "_").replace("-", "_")
        if "codeqwen" in path.name.lower():
            return ModelDescriptor(
                model_id=stem,
                label=path.stem,
                artifact_path=str(path),
                adapter_kind="codeqwen_lora",
                prompt_format="Qwen chat template with Context/Question user message",
                notes="PEFT LoRA adapter; requires Qwen/CodeQwen1.5-7B-Chat and GPU-class resources.",
            )
        return ModelDescriptor(
            model_id=stem,
            label=path.stem,
            artifact_path=str(path),
            adapter_kind="codet5_seq2seq",
            prompt_format="question: {question} context: {ddl}",
            notes="Seq2seq model artifact from CodeT5+/T5 notebooks.",
        )

    def _extract(self, descriptor: ModelDescriptor) -> Path:
        if descriptor.artifact_path is None:
            raise ValueError(f"Model {descriptor.model_id} does not have a zip artifact")
        target = self.settings.model_cache_dir / descriptor.model_id
        marker = target / ".extracted"
        if marker.exists():
            return target
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(descriptor.artifact_path) as archive:
            archive.extractall(target)
        marker.write_text("ok", encoding="utf-8")
        return target

    def _model_root(self, artifact_path: Path) -> Path:
        if (artifact_path / "config.json").exists() or (artifact_path / "adapter_config.json").exists():
            return artifact_path
        candidates = [
            path
            for path in artifact_path.rglob("*")
            if path.is_dir() and ((path / "config.json").exists() or (path / "adapter_config.json").exists())
        ]
        if not candidates:
            raise FileNotFoundError(f"No model config found under {artifact_path}")
        return candidates[0]


def heuristic_sql(question: str, tables: list[SchemaTable]) -> str:
    """Small deterministic fallback so the Dagents demo works without GPU models."""
    if not tables:
        return "SELECT 1"
    question_l = question.lower()
    table_columns = {
        normalize_identifier(table.name): [normalize_identifier(column.name) for column in table.columns]
        for table in tables
    }
    first_table = next(iter(table_columns))
    first_columns = table_columns[first_table]

    if "how many" in question_l or "count" in question_l:
        select_clause = "COUNT(*)"
    else:
        mentioned: list[str] = []
        for table_name, columns in table_columns.items():
            for column in columns:
                if _mentions_column(question_l, column):
                    mentioned.append(_qualified_column(table_name, column, len(tables) > 1))
        select_clause = ", ".join(mentioned[:4] or first_columns[: min(3, len(first_columns))] or ["*"])

    sql = f"SELECT {select_clause} FROM {first_table}"
    if len(tables) > 1:
        for table in list(table_columns)[1:]:
            join_column = next((column for column in table_columns[table] if column in first_columns), None)
            if join_column:
                sql += f" JOIN {table} ON {first_table}.{join_column} = {table}.{join_column}"
            else:
                sql += f", {table}"

    predicates: list[str] = []
    question_tokens = set(re.findall(r"[a-z0-9_]+", question_l))
    for value, column_hints in {
        "paid": ["status"],
        "unpaid": ["status"],
        "open": ["status"],
        "closed": ["status"],
        "critical": ["severity", "priority"],
        "high": ["severity", "priority"],
        "alpha": ["tenant_id", "tenant"],
        "beta": ["tenant_id", "tenant"],
        "review": ["status"],
    }.items():
        if value in question_tokens:
            column_ref = _find_predicate_column(table_columns, column_hints)
            if column_ref:
                predicates.append(f"{column_ref} = '{value}'")
    if predicates:
        sql += " WHERE " + " AND ".join(dict.fromkeys(predicates))
    return sql


def _mentions_column(question_l: str, column: str) -> bool:
    column_l = column.lower()
    words = column_l.replace("_", " ")
    singular_words = words.removesuffix("s")
    tokens = set(re.findall(r"[a-z0-9]+", question_l))
    return (
        column_l in question_l
        or words in question_l
        or singular_words in question_l
        or all(token in tokens or f"{token}s" in tokens for token in words.split())
    )


def _qualified_column(table_name: str, column: str, qualify: bool) -> str:
    return f"{table_name}.{column}" if qualify else column


def _find_predicate_column(table_columns: dict[str, list[str]], hints: list[str]) -> str | None:
    qualify = len(table_columns) > 1
    for hint in hints:
        for table_name, columns in table_columns.items():
            for column in columns:
                if column == hint or hint in column:
                    return _qualified_column(table_name, column, qualify)
    return None
