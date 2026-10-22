# Top candidate S1+E4 - validation - 60m bars, recorded costs (EXPLORATORY)

- split: validation
- inputs hash: `e9e9d1a2bb46c399511ac634710eece987acc32846f9ad397f5afcb83d94c129`
- result hash: `747dcc419c6e22f810ea38a16a8a12e44c5f674f09f56aef9f4949dc176b0cf2`
- git SHA: 6111708d3dac542f497ad863bd40a69615b5a497
- symbols: BTCUSDT, ETHUSDT, PEPEUSDT, SOLUSDT, USOONUSDT, XAUTUSDT, XRPUSDT
- bars: 306 x 3600s

## Metrics

| metric | value |
|---|---|
| initial_equity | 10000 |
| final_equity | 10000 |
| net_return | 0 |
| max_drawdown | 0 |
| bars | 231 |
| bar_interval_s | 3600 |
| periods_per_year | 8760 |
| annualized_return | 0 |
| annualized_volatility | 0 |
| sharpe | n/a |
| trade_count | 0 |
| wins | 0 |
| losses | 0 |
| win_rate | n/a |
| avg_win | n/a |
| avg_loss | n/a |
| expectancy_quote | n/a |
| expectancy_r | n/a |
| profit_factor | n/a |
| net_pnl | 0 |
| gross_pnl | 0 |
| fees | 0 |
| spread_cost | 0 |
| impact_cost | 0 |
| fee_share_of_gross | n/a |
| spread_share_of_gross | n/a |
| slippage_share_of_gross | n/a |
| cost_bps_of_traded_notional | n/a |
| traded_notional | 0 |
| turnover | 0 |
| exposure_time | 0 |
| avg_exposure | 0 |
| end_of_data_trades | 0 |

## Parameters

```json
{
  "bar_minutes": 60,
  "cost_labels": {
    "BTCUSDT": "recorded (p90)",
    "ETHUSDT": "recorded (p90)",
    "PEPEUSDT": "recorded (p90)",
    "SOLUSDT": "recorded (p90)",
    "USOONUSDT": "recorded (p90)",
    "XAUTUSDT": "recorded (p90)",
    "XRPUSDT": "recorded (p90)"
  },
  "cost_source": "recorded",
  "engine_inputs_hash": "e9e9d1a2bb46c399511ac634710eece987acc32846f9ad397f5afcb83d94c129",
  "engine_result_hash": "957a48a82a1cc3415458fa5fd143ab90a489c048a1fedd40fdd97a903ae69418",
  "exit": "E4",
  "exit_params": {
    "atr_period": 14,
    "breakeven_buffer_bps": "20",
    "chandelier_mult": "2",
    "ema_period": 9,
    "false_breakout_bars": 2,
    "fee_rate": "0.0035",
    "indicator_window_mult": 5,
    "partial_fraction": "0.5",
    "partial_r": "1",
    "roc_period": 3,
    "time_stop_bars": 16,
    "time_stop_only_without_partial": true,
    "tp_r": "1.5",
    "trail_activation_r": "1",
    "volume_dry_ratio": "0.5",
    "volume_lookback": 20
  },
  "initial_equity": "10000",
  "momentum": {
    "atr_pct_max": "0.03",
    "atr_pct_min": "0.002",
    "atr_period": "14",
    "cooldown_bars": "8",
    "cooldown_bars_profitable": "2",
    "cost_mult": "3.0",
    "cost_mult_tight": "4.0",
    "donchian_period": "20",
    "ema_period": "50",
    "ema_slope_bars": "4",
    "er_min": "0.3",
    "er_period": "20",
    "expected_move_atr": "1.5",
    "history_bars": "300",
    "late_atr_mult": "1.0",
    "low_window": "24",
    "max_advance_atr": "5.0",
    "max_zero_volume_frac": "0.2",
    "min_bars": "120",
    "min_score": "0.6",
    "roc_period": "6",
    "roc_z_min": "1.0",
    "roc_z_window": "96",
    "rsi_max": "85.0",
    "rsi_period": "14",
    "stop_atr_mult": "1.5",
    "vol_median_window": "48",
    "volume_mult": "1.8",
    "volume_mult_tight": "2.2"
  },
  "risk": {
    "consecutive_loss_cooldown": 3,
    "daily_loss_hard_pct": "6",
    "daily_loss_soft_pct": "4",
    "drawdown_hard_pct": "12",
    "drawdown_soft_pct": "8",
    "max_exposure_pct": "60",
    "max_open_positions": 3,
    "max_order_value": "1000",
    "max_position_pct": "25",
    "min_notional": "5",
    "risk_per_trade_pct": "1"
  },
  "split_note": "50% train / 25% validation / remainder (~25%) test, by 15m bar time; embargo 300 bars between segments; 300-bar warm-up from the embargo.",
  "strategy": "S1",
  "taker_rate": "0.0035",
  "warmup_trimmed_equity_view": true
}
```

## Limitations

EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability. The universe was ranked by CURRENT 24h volume (selection bias). Window ~45 days. Costs: per-symbol p90 half spread and a p90 impact curve calibrated from a few hours of recorded order books (one or two sessions, top 10 levels; NOT representative of all hours or regimes; see docs/COSTS.md); symbols without a usable calibration use the proxy model and are labelled. Fills at next-bar open/level touch; take-profits also pay taker costs. Fees are placeholders/assumptions charged in quote (Wallex fee unverified). The S1 cost gate sees the recorded full spread plus the round-trip impact at 1000 USDT. 60m bars over ~45 days give few bars per segment (S0/S1 need 50-120 bars of history first) and therefore few trades and low statistical power. Warm-up bars before each segment are excluded from metrics. Missing-minute bars are kept and counted. No parameters were tuned.
