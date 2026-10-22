# Top candidate S1+E4 - validation - 15m bars, recorded costs (EXPLORATORY)

- split: validation
- inputs hash: `8d616be7ac8c8d7b038a3b3674c34ab16b6e9ffc832bd8398d601d61e6fc0260`
- result hash: `474613e6746c93cbf21b7f93f6106a78fbd15517020783d5c2dc361c552369b4`
- git SHA: 6111708d3dac542f497ad863bd40a69615b5a497
- symbols: BTCUSDT, ETHUSDT, PEPEUSDT, SOLUSDT, USOONUSDT, XAUTUSDT, XRPUSDT
- bars: 1228 x 900s

## Metrics

| metric | value |
|---|---|
| initial_equity | 10000 |
| final_equity | 10000 |
| net_return | 0 |
| max_drawdown | 0 |
| bars | 928 |
| bar_interval_s | 900 |
| periods_per_year | 35040 |
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
  "bar_minutes": 15,
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
  "engine_inputs_hash": "8d616be7ac8c8d7b038a3b3674c34ab16b6e9ffc832bd8398d601d61e6fc0260",
  "engine_result_hash": "17f1c7036c7773ada97bb3a8b25390269840aeeadf1a6d027283ee98e278d9a2",
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

EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability. The universe was ranked by CURRENT 24h volume (selection bias). Window ~45 days. Costs: per-symbol p90 half spread and a p90 impact curve calibrated from a few hours of recorded order books (one or two sessions, top 10 levels; NOT representative of all hours or regimes; see docs/COSTS.md); symbols without a usable calibration use the proxy model and are labelled. Fills at next-bar open/level touch; take-profits also pay taker costs. Fees are placeholders/assumptions charged in quote (Wallex fee unverified). The S1 cost gate sees the recorded full spread plus the round-trip impact at 1000 USDT. Warm-up bars before each segment are excluded from metrics. Missing-minute bars are kept and counted. No parameters were tuned.
