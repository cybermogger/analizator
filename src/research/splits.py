from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .types import ResearchRow


@dataclass(frozen=True)
class SplitFold:
    fold: int
    train_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    calibration_ids: tuple[str, ...]
    test_ids: tuple[str, ...]
    validation_start_ms: int
    calibration_start_ms: int
    test_start_ms: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SplitPlan:
    folds: tuple[SplitFold, ...]
    embargo_ms: int
    purge_by_label_end: bool
    untouched_test_ids: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "embargo_ms": self.embargo_ms,
            "purge_by_label_end": self.purge_by_label_end,
            "untouched_test_ids": list(self.untouched_test_ids),
            "folds": [fold.to_dict() for fold in self.folds],
        }


class PurgedWalkForwardSplitter:
    """Expanding train with validation/calibration blocks and one fixed final test."""

    def __init__(
        self,
        *,
        folds: int = 3,
        test_fraction: float = 0.20,
        minimum_train_rows: int = 100,
        minimum_partition_rows: int = 20,
        embargo_ms: int = 0,
    ) -> None:
        if folds < 1:
            raise ValueError("folds must be positive")
        if not 0 < test_fraction < 0.5:
            raise ValueError("test_fraction must be between 0 and 0.5")
        self.folds = folds
        self.test_fraction = test_fraction
        self.minimum_train_rows = minimum_train_rows
        self.minimum_partition_rows = minimum_partition_rows
        self.embargo_ms = embargo_ms

    def split(self, source: Iterable[ResearchRow]) -> SplitPlan:
        rows = sorted(source, key=lambda row: (row.decision_at_ms, row.row_id))
        required = self.minimum_train_rows + self.folds * self.minimum_partition_rows * 2
        if len(rows) < required:
            raise ValueError(f"insufficient rows for purged walk-forward: {len(rows)} < {required}")
        test_index = max(required, int(len(rows) * (1.0 - self.test_fraction)))
        if test_index >= len(rows):
            raise ValueError("insufficient untouched test rows")
        test_start = rows[test_index].decision_at_ms
        test = tuple(row.row_id for row in rows if row.decision_at_ms >= test_start + self.embargo_ms)
        if len(test) < self.minimum_partition_rows:
            raise ValueError("untouched test partition is too small after embargo")

        development_width = test_index - self.minimum_train_rows
        block_width = development_width // self.folds
        if block_width < self.minimum_partition_rows * 2:
            raise ValueError("validation/calibration blocks are too small")

        result: list[SplitFold] = []
        for fold_index in range(self.folds):
            block_start = self.minimum_train_rows + fold_index * block_width
            block_end = test_index if fold_index == self.folds - 1 else block_start + block_width
            middle = block_start + (block_end - block_start) // 2
            validation_start = rows[block_start].decision_at_ms
            calibration_start = rows[middle].decision_at_ms
            fold_end = test_start if block_end >= test_index else rows[block_end].decision_at_ms

            train = tuple(
                row.row_id for row in rows[:block_start]
                if row.label_end_at_ms < validation_start
            )
            validation = tuple(
                row.row_id for row in rows[block_start:middle]
                if row.decision_at_ms >= validation_start + self.embargo_ms
                and row.label_end_at_ms < calibration_start
            )
            calibration = tuple(
                row.row_id for row in rows[middle:block_end]
                if row.decision_at_ms >= calibration_start + self.embargo_ms
                and row.label_end_at_ms < fold_end
            )
            for name, values in (
                ("train", train), ("validation", validation), ("calibration", calibration)
            ):
                if len(values) < self.minimum_partition_rows:
                    raise ValueError(f"fold {fold_index} {name} too small after purge/embargo")
            split = SplitFold(
                fold=fold_index,
                train_ids=train,
                validation_ids=validation,
                calibration_ids=calibration,
                test_ids=test,
                validation_start_ms=validation_start,
                calibration_start_ms=calibration_start,
                test_start_ms=test_start,
            )
            self._assert_disjoint(split)
            result.append(split)
        return SplitPlan(tuple(result), self.embargo_ms, True, test)

    @staticmethod
    def _assert_disjoint(fold: SplitFold) -> None:
        partitions = [
            set(fold.train_ids), set(fold.validation_ids),
            set(fold.calibration_ids), set(fold.test_ids),
        ]
        for index, left in enumerate(partitions):
            for right in partitions[index + 1:]:
                if left.intersection(right):
                    raise AssertionError("walk-forward partitions overlap")
