"""Forward cash forecasting: booked outflows plus a modelled, calibrated residual."""

from ledger.forecasting.backtest import (
    BacktestResult,
    HorizonScore,
    backtest,
)
from ledger.forecasting.history import (
    DailyFlow,
    build_cash_history,
    closing_position,
    summarise,
)
from ledger.forecasting.model import (
    BookedOutflow,
    CashForecaster,
    Forecast,
    ForecastPoint,
    InsufficientHistoryError,
)

__all__ = [
    "BacktestResult",
    "BookedOutflow",
    "CashForecaster",
    "DailyFlow",
    "Forecast",
    "ForecastPoint",
    "HorizonScore",
    "InsufficientHistoryError",
    "backtest",
    "build_cash_history",
    "closing_position",
    "summarise",
]
