import React, { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { AreaChart, Area, LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, ScatterChart, Scatter, ComposedChart, Bar } from 'recharts'
import { useAuth } from '../contexts/AuthContext'
import { api, getAccessToken } from '../api/axios'

// ── Types ─────────────────────────────────────────────────────────────────────

interface BrokerBotStatus {
  running: boolean
  in_trade: boolean
  entry_price: number | null
  trade_side: string | null
  trail_stop: number | null
  last_signal: string | null
  last_update: string | null
  error_count: number
  demo_mode?: boolean
  key_invalid?: boolean
  indicators?: Record<string, any>
  position_size?: number
  symbols?: string[]
  risk?: {
    is_paused: boolean
    pause_reason: string
    daily_pnl: number
    daily_drawdown_pct: number
    cooldown_remaining: number
    recent_stops: number
    max_drawdown_pct: number
    max_stops: number
  }
}

interface BotStatusMap {
  robinhood: BrokerBotStatus
  capital: BrokerBotStatus
  // backward-compat top-level fields
  running?: boolean
  in_trade?: boolean
  demo_mode?: boolean
  key_invalid?: boolean
}

interface BrokerBalances {
  robinhood: { available: number; is_demo: boolean }
  capital: { available: number; is_demo: boolean }
}

// Legacy alias — keeps older references compiling
type BotStatus = BrokerBotStatus

interface Trade {
  id: string
  symbol: string
  side: string
  quantity: string
  entry_price: string | null
  exit_price: string | null
  pnl: number | null
  pnl_pct: number | null
  state: string
  is_demo: boolean
  exit_reason: string | null
  is_system_close?: boolean
  opened_at: string | null
  closed_at: string | null
  indicators_snapshot?: string
  ai: {
    grade: string
    entry_quality: string
    exit_quality: string
    what_went_well: string[]
    what_went_wrong: string[]
    improvements: string[]
    confidence: number
    analyzed: boolean
  } | null
}

interface Stats {
  total: number
  wins: number
  losses: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
  pnl_chart: { date: string; pnl: number }[]
}

interface DailyReport {
  report_date: string
  total_trades: number
  wins: number
  losses: number
  total_pnl: number
  win_rate: number
  summary: string
  top_improvement: string
  full_report: {
    patterns_noticed?: string[]
    risk_assessment?: string
    recommendation?: string
  }
}

interface LiveEvent {
  id: string
  type: string
  message: string
  color: string
  time: string
}

interface Settings {
  trading_symbol: string
  entry_z: number
  lookback: string
  stop_loss_pct: number
  take_profit_pct: number
  trail_stop_pct: number
  has_api_keys: boolean
  demo_mode?: boolean
  public_key?: string
  demo_balance?: number
  use_rsi_filter?: boolean
  use_ema_filter?: boolean
  use_adx_filter?: boolean
  use_bbands_filter?: boolean
  use_macd_filter?: boolean
  use_volume_filter?: boolean
  max_drawdown_pct?: number
  max_stops_before_pause?: number
  cooldown_ticks?: number
  risk_per_trade_pct?: number
  max_exposure_pct?: number
  position_size_mode?: string
  fixed_quantity?: number
  telegram_enabled?: boolean
  telegram_configured?: boolean
  broker_type?: string
  has_capital_keys?: boolean
  has_tradovate_keys?: boolean
  capital_demo_balance?: number
  bot_active_capital?: boolean
}

interface Balance {
  available: number | null
  holdings: { asset_code: string; total_quantity: string; quantity_available_for_trading: string }[]
  error?: string
}

// ── Small components ──────────────────────────────────────────────────────────

function GradeBadge({ grade }: { grade?: string | null }) {
  if (!grade || grade === 'N/A')
    return <span className="text-xs px-2 py-0.5 rounded-full bg-border text-muted">N/A</span>
  const styles: Record<string, string> = {
    A: 'bg-profit/20 text-profit', B: 'bg-accent/20 text-accent',
    C: 'bg-warning/20 text-warning', D: 'bg-orange-500/20 text-orange-400',
    F: 'bg-loss/20 text-loss',
  }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-bold ${styles[grade] ?? 'bg-border text-muted'}`}>{grade}</span>
}

function Toast({ msg, ok }: { msg: string; ok: boolean }) {
  return (
    <div className={`fixed bottom-6 right-6 z-50 px-5 py-3 rounded-xl shadow-xl text-sm font-medium slide-in ${ok ? 'bg-profit' : 'bg-loss'} text-white`}>
      {msg}
    </div>
  )
}

function TabButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`px-4 py-2 text-xs font-semibold rounded-lg transition-colors ${active ? 'bg-accent/20 text-accent border border-accent/30' : 'text-muted hover:text-white hover:bg-elevated'}`}
    >
      {label}
    </button>
  )
}

function IndicatorPill({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="px-3 py-1.5 rounded-lg bg-elevated border border-border/50 text-xs">
      <span className="text-muted">{label}: </span>
      <span className={`font-mono font-semibold ${color ?? 'text-white'}`}>{value}</span>
    </div>
  )
}

function BotCard({ broker, label, icon, symbols, status, loading, hasKeys, onStart, onStop, balance, balanceLabel }: {
  broker: string; label: string; icon: string; symbols: string[]
  status: BrokerBotStatus | null; loading: boolean; hasKeys: boolean
  onStart: (mode: 'demo' | 'live') => void; onStop: () => void
  balance: number | null; balanceLabel: string
}) {
  const running = status?.running ?? false
  const isDemo = status?.demo_mode ?? true
  return (
    <div className={`rounded-2xl border p-4 flex flex-col gap-3 ${running ? (isDemo ? 'border-warning/40 bg-warning/5' : 'border-profit/40 bg-profit/5') : 'border-border bg-elevated'}`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-lg">{icon}</span>
          <div>
            <p className="font-semibold text-sm text-white">{label}</p>
            <p className="text-xs text-muted">{symbols.join(' · ')}</p>
          </div>
        </div>
        <div className={`px-2 py-1 rounded-full text-xs font-bold border ${
          running ? (isDemo ? 'text-warning border-warning/40 bg-warning/10' : 'text-profit border-profit/40 bg-profit/10')
                  : 'text-muted border-border bg-elevated'}`}>
          {running ? (isDemo ? 'DEMO' : 'LIVE') : 'OFF'}
        </div>
      </div>

      {/* Balance */}
      <div className="rounded-xl bg-surface/60 px-3 py-2 flex items-center justify-between" style={{ backgroundColor: 'rgba(255,255,255,0.04)' }}>
        <span className="text-xs text-muted">{balanceLabel}</span>
        <span className="font-mono font-bold text-sm text-white">
          {balance != null ? `$${balance.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—'}
        </span>
      </div>

      {/* Signal */}
      {running && status?.last_signal && (
        <p className="text-xs text-muted/80 truncate">{status.last_signal.startsWith('Warming') ? 'Analyzing market data...' : status.last_signal.startsWith('Paused:') ? status.last_signal : 'Scanning for opportunities...'}</p>
      )}

      {/* Controls */}
      <div className="flex gap-2 mt-auto">
        {running ? (
          <button onClick={onStop} disabled={loading}
            className="flex-1 px-3 py-2 rounded-lg bg-loss/20 border border-loss/40 text-loss text-xs font-semibold hover:bg-loss/30 transition-colors disabled:opacity-50">
            {loading ? '...' : 'Stop'}
          </button>
        ) : (
          <>
            <button onClick={() => onStart('demo')} disabled={loading}
              className="flex-1 px-3 py-2 rounded-lg bg-warning/15 border border-warning/40 text-warning text-xs font-semibold hover:bg-warning/25 transition-colors disabled:opacity-50">
              {loading ? '...' : 'Start Demo'}
            </button>
            <button onClick={() => onStart('live')} disabled={loading || !hasKeys}
              title={!hasKeys ? 'Add API keys in Settings first' : 'Start live trading'}
              className="flex-1 px-3 py-2 rounded-lg bg-profit/15 border border-profit/40 text-profit text-xs font-semibold hover:bg-profit/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
              {loading ? '...' : 'Start Live'}
            </button>
          </>
        )}
      </div>
    </div>
  )
}

// Format a crypto quantity with sensible decimals: BTC shows 6dp, ETH 4dp,
// DOGE 0-2dp. Trims trailing zeros so "1.50000000" → "1.5".
function formatQty(q: string | number | null | undefined): string {
  if (q === null || q === undefined || q === '') return '—'
  const n = typeof q === 'string' ? parseFloat(q) : q
  if (!Number.isFinite(n)) return '—'
  const abs = Math.abs(n)
  let dp: number
  if (abs >= 1000) dp = 2
  else if (abs >= 1) dp = 4
  else if (abs >= 0.01) dp = 6
  else dp = 8
  // toFixed then strip trailing zeros (but keep at least 2 decimals for readability)
  const fixed = n.toFixed(dp)
  return fixed.replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1')
}

// ── Main Dashboard ────────────────────────────────────────────────────────────

export function Dashboard() {
  const { user, logout, refreshUser } = useAuth()
  const navigate = useNavigate()

  const [activeTab, setActiveTab] = useState<'dashboard' | 'trades' | 'analytics' | 'settings'>('dashboard')
  const [botStatus, setBotStatus] = useState<BotStatusMap | null>(null)
  const [brokerBalances, setBrokerBalances] = useState<BrokerBalances | null>(null)
  const [capitalDemoBalance, setCapitalDemoBalance] = useState<number | null>(null)
  const [livePrice, setLivePrice] = useState<number | null>(null)
  const [liveZ, setLiveZ] = useState<number | null>(null)
  const [trades, setTrades] = useState<Trade[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [report, setReport] = useState<DailyReport | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [expandedTrade, setExpandedTrade] = useState<string | null>(null)
  const [tradeFilter, setTradeFilter] = useState<'all' | 'live' | 'demo'>('all')
  const [perfMode, setPerfMode] = useState<'all' | 'live' | 'demo'>('all')
  const [demoStats, setDemoStats] = useState<Stats | null>(null)
  const [liveStats, setLiveStats] = useState<Stats | null>(null)
  // Per-broker all-time stats — populates the broker breakdown card.
  // Today's P&L per broker comes from rhStatus.risk.daily_pnl / capStatus.risk.daily_pnl
  // (server-side tracked independently for each broker — never mixed).
  const [rhStats, setRhStats] = useState<Stats | null>(null)
  const [capStats, setCapStats] = useState<Stats | null>(null)
  const [feed, setFeed] = useState<LiveEvent[]>([])
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null)
  const [botLoading, setBotLoading] = useState(false)
  const [botLoadingRh, setBotLoadingRh] = useState(false)
  const [botLoadingCap, setBotLoadingCap] = useState(false)
  const [brokerFilter, setBrokerFilter] = useState<'all' | 'robinhood' | 'capital'>('all')
  // Daily compounding target
  const [dailyPnl, setDailyPnl] = useState(0)
  const [dailyTarget, setDailyTarget] = useState(200)
  const [dailyProgressPct, setDailyProgressPct] = useState(0)
  const [settingsLoading, setSettingsLoading] = useState(false)
  const [keysLoading, setKeysLoading] = useState(false)
  const [balance, setBalance] = useState<Balance | null>(null)  // kept for legacy /balance endpoint
  const [showConfirm, setShowConfirm] = useState(false)
  const [pendingSettings, setPendingSettings] = useState<null | Record<string, any>>(null)

  const [testLoading, setTestLoading] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const [demoBalanceInput, setDemoBalanceInput] = useState('10000')
  const [demoBalanceLoading, setDemoBalanceLoading] = useState(false)
  const [liveDemoBalance, setLiveDemoBalance] = useState<number | null>(null)
  const [priceHistory, setPriceHistory] = useState<{ time: string; price: number; z?: number }[]>([])
  const [equityCurve, setEquityCurve] = useState<{ time: string; balance: number }[]>([])
  const [tradeMarkers, setTradeMarkers] = useState<{ time: string; price: number; side: string }[]>([])
  // Premium
  const [isPremium, setIsPremium] = useState(false)
  const [premiumData, setPremiumData] = useState<any>(null)
  const [premiumLoading, setPremiumLoading] = useState(false)
  const [calibrations, setCalibrations] = useState<any[]>([])
  const [showPremiumModal, setShowPremiumModal] = useState(false)

  // Settings form state
  const [rhApiKey, setRhApiKey] = useState('')
  const [formSymbol, setFormSymbol] = useState('BTC-USD')
  const [formEntryZ, setFormEntryZ] = useState(2.0)
  const [formLookback, setFormLookback] = useState(20)
  const [formStopLoss, setFormStopLoss] = useState(0.025)
  const [formTakeProfit, setFormTakeProfit] = useState(0.05)
  const [formTrailStop, setFormTrailStop] = useState(0.015)
  // Indicator filters
  const [formRsi, setFormRsi] = useState(true)
  const [formEma, setFormEma] = useState(true)
  const [formAdx, setFormAdx] = useState(true)
  const [formBbands, setFormBbands] = useState(true)
  const [formMacd, setFormMacd] = useState(false)
  const [formVolume, setFormVolume] = useState(false)
  // Risk management
  const [formMaxDrawdown, setFormMaxDrawdown] = useState(5.0)
  const [formMaxStops, setFormMaxStops] = useState(3)
  const [formCooldown, setFormCooldown] = useState(5)
  const [formRiskPerTrade, setFormRiskPerTrade] = useState(1.0)
  const [formMaxExposure, setFormMaxExposure] = useState(20.0)
  // Position sizing
  const [formPosMode, setFormPosMode] = useState('dynamic')
  const [formFixedQty, setFormFixedQty] = useState(0.0001)
  // Telegram
  const [formTelegramEnabled, setFormTelegramEnabled] = useState(false)
  const [telegramToken, setTelegramToken] = useState('')
  const [telegramChatId, setTelegramChatId] = useState('')
  const [telegramLoading, setTelegramLoading] = useState(false)
  // Broker selector
  const [formBroker, setFormBroker] = useState<'robinhood' | 'capital' | 'tradovate'>('robinhood')
  const [brokerKeysTab, setBrokerKeysTab] = useState<'robinhood' | 'capital' | 'tradovate'>('robinhood')
  // Capital.com credentials form
  const [capitalApiKey, setCapitalApiKey] = useState('')
  const [capitalIdentifier, setCapitalIdentifier] = useState('')
  const [capitalPassword, setCapitalPassword] = useState('')
  const [capitalKeyLoading, setCapitalKeyLoading] = useState(false)
  const [capitalTestLoading, setCapitalTestLoading] = useState(false)
  const [capitalTestResult, setCapitalTestResult] = useState<{ ok: boolean; msg: string } | null>(null)
  // Tradovate credentials form
  const [tradovateUsername, setTradovateUsername] = useState('')
  const [tradovatePassword, setTradovatePassword] = useState('')
  const [tradovateAccountId, setTradovateAccountId] = useState('')
  const [tradovateKeyLoading, setTradovateKeyLoading] = useState(false)
  const [tradovateTestLoading, setTradovateTestLoading] = useState(false)
  const [tradovateTestResult, setTradovateTestResult] = useState<{ ok: boolean; msg: string } | null>(null)

  const wsRef = useRef<WebSocket | null>(null)
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const wsAuthFailedRef = useRef(false)
  // Primary-symbol ref: keeps the latest trading_symbol available inside ws.onmessage
  // without re-creating the WebSocket whenever settings changes. Status ticks for
  // OTHER symbols (the parallel BTC/ETH/SOL/DOGE bots) must NOT overwrite the header.
  const primarySymbolRef = useRef<string>('BTC-USD')

  const showToast = useCallback((msg: string, ok = true) => {
    setToast({ msg, ok })
    setTimeout(() => setToast(null), 3500)
  }, [])

  const addFeed = useCallback((type: string, message: string, color: string) => {
    setFeed(prev =>
      [{ id: Math.random().toString(36).slice(2), type, message, color, time: new Date().toLocaleTimeString() },
       ...prev].slice(0, 12)
    )
  }, [])

  const loadData = useCallback(async () => {
    const modeParam = tradeFilter !== 'all' ? `&mode=${tradeFilter}` : ''
    const brokerParam = brokerFilter !== 'all' ? `&broker=${brokerFilter}` : ''
    const results = await Promise.allSettled([
      api.get('/api/bot/status'),
      api.get(`/api/trades?limit=20${modeParam}${brokerParam}`),
      api.get(`/api/trades/stats?mode=${tradeFilter}${brokerParam}`),
      api.get('/api/reports/latest'),
      api.get('/api/bot/settings'),
      api.get('/api/trades/stats?mode=demo'),
      api.get('/api/trades/stats?mode=live'),
      api.get('/api/trades/stats?broker=robinhood'),
      api.get('/api/trades/stats?broker=capital'),
    ])
    const [statusR, tradesR, statsR, reportR, settingsR, demoStatsR, liveStatsR, rhStatsR, capStatsR] = results
    if (statusR.status === 'fulfilled') setBotStatus(statusR.value.data as BotStatusMap)
    if (tradesR.status === 'fulfilled') setTrades(tradesR.value.data)
    if (statsR.status === 'fulfilled') setStats(statsR.value.data)
    if (demoStatsR.status === 'fulfilled') setDemoStats(demoStatsR.value.data)
    if (liveStatsR.status === 'fulfilled') setLiveStats(liveStatsR.value.data)
    if (rhStatsR.status === 'fulfilled') setRhStats(rhStatsR.value.data)
    if (capStatsR.status === 'fulfilled') setCapStats(capStatsR.value.data)
    if (reportR.status === 'fulfilled') setReport(reportR.value.data)
    if (settingsR.status === 'fulfilled') {
      const s: Settings = settingsR.value.data
      setSettings(s)
      setFormSymbol(s.trading_symbol)
      setFormStopLoss(s.stop_loss_pct)
      setFormTakeProfit(s.take_profit_pct)
      setFormTrailStop(s.trail_stop_pct)
      setDemoBalanceInput(String(s.demo_balance ?? 10000))
      if (!liveDemoBalance) setLiveDemoBalance(s.demo_balance ?? 10000)
      if (s.capital_demo_balance && !capitalDemoBalance) setCapitalDemoBalance(s.capital_demo_balance)
      // Risk
      setFormMaxDrawdown(s.max_drawdown_pct ?? 5.0)
      setFormMaxStops(s.max_stops_before_pause ?? 3)
      setFormCooldown(s.cooldown_ticks ?? 5)
      setFormRiskPerTrade(s.risk_per_trade_pct ?? 1.0)
      setFormMaxExposure(s.max_exposure_pct ?? 20.0)
      // Position
      setFormPosMode(s.position_size_mode ?? 'dynamic')
      setFormFixedQty(s.fixed_quantity ?? 0.0001)
      // Telegram
      setFormTelegramEnabled(s.telegram_enabled ?? false)
      // Broker
      const broker = (s.broker_type as 'robinhood' | 'capital' | 'tradovate') ?? 'robinhood'
      setFormBroker(broker)
      setBrokerKeysTab(broker)

      api.get('/api/bot/balance').then(r => {
        setBalance(r.data)
        if (r.data.is_demo && r.data.available) setLiveDemoBalance(r.data.available)
      }).catch(() => {})
    }
    // Fetch both-broker balances
    api.get('/api/bot/balances').then(r => setBrokerBalances(r.data)).catch(() => {})
    // Fetch premium status
    api.get('/api/bot/premium/status').then(r => {
      setIsPremium(r.data.is_premium)
      setPremiumData(r.data)
      if (r.data.recent_calibrations) setCalibrations(r.data.recent_calibrations)
    }).catch(() => {})
  }, [tradeFilter, brokerFilter])

  // Refresh broker balances every 30s so the live broker balance stays current.
  // Critical in live mode — without this, the dashboard shows a stale snapshot
  // and a winning trade's cash gain doesn't appear until the next page load.
  useEffect(() => {
    const tick = () => {
      api.get('/api/bot/balances').then(r => setBrokerBalances(r.data)).catch(() => {})
    }
    const id = setInterval(tick, 30000)
    return () => clearInterval(id)
  }, [])

  // Build equity curve from trades, filtered by perfMode
  useEffect(() => {
    if (!trades.length) return
    const startBal = settings?.demo_balance ?? 10000
    let bal = startBal
    const curve: { time: string; balance: number }[] = []
    const markers: { time: string; price: number; side: string }[] = []
    const filteredTrades = [...trades].filter(t => {
      if (t.state !== 'closed' || t.pnl === null) return false
      if (perfMode === 'demo') return t.is_demo
      if (perfMode === 'live') return !t.is_demo
      return true
    }).reverse()
    filteredTrades.forEach(t => {
      bal += (t.pnl ?? 0)
      curve.push({ time: t.closed_at ? new Date(t.closed_at).toLocaleDateString() : '', balance: Math.round(bal * 100) / 100 })
      if (t.entry_price) markers.push({ time: t.opened_at ? new Date(t.opened_at).toLocaleTimeString() : '', price: parseFloat(t.entry_price), side: t.side })
    })
    setEquityCurve(curve)
    setTradeMarkers(markers)
  }, [trades, perfMode])

  // WebSocket
  const connectWS = useCallback(() => {
    const token = getAccessToken()
    if (!token) return
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/ws?token=${token}`)
    wsRef.current = ws

    ws.onopen = () => addFeed('connected', 'WebSocket connected', 'text-accent')

    ws.onmessage = ev => {
      try {
        const d = JSON.parse(ev.data)
        if (d.type === 'status_update') {
          // Per-user bots run multiple parallel symbol loops (BTC/ETH/SOL/DOGE).
          // Only ticks for the user's PRIMARY trading_symbol drive the header
          // price/z/in-trade UI — other-symbol ticks would cause flicker.
          const isPrimary = !d.symbol || d.symbol === primarySymbolRef.current
          // Account-level fields (demo_balance, daily_pnl, daily_target) are global
          // across all loops, so always accept them regardless of symbol.
          if (d.demo_balance != null) {
            if (d.balance_broker === 'capital') {
              setCapitalDemoBalance(d.demo_balance)
            } else {
              setLiveDemoBalance(d.demo_balance)
            }
          }
          if (typeof d.daily_pnl === 'number') setDailyPnl(d.daily_pnl)
          if (typeof d.daily_target === 'number') setDailyTarget(d.daily_target)
          if (typeof d.daily_progress_pct === 'number') setDailyProgressPct(d.daily_progress_pct)
          if (!isPrimary) return
          if (d.price) setLivePrice(d.price)
          if (typeof d.z_score === 'number') setLiveZ(d.z_score)
          if (d.price) {
            setPriceHistory(prev => [...prev.slice(-120), { time: new Date().toLocaleTimeString(), price: d.price, z: d.z_score }])
          }
          setBotStatus(prev => {
            if (!prev) return prev
            const broker: 'robinhood' | 'capital' = d.broker === 'capital' ? 'capital' : 'robinhood'
            const brokerState = prev[broker] ?? {}
            const updatedBroker: BrokerBotStatus = {
              ...brokerState,
              running: true,
              in_trade: d.in_trade,
              entry_price: d.entry_price,
              trade_side: d.trade_side,
              trail_stop: d.trail_stop,
              last_signal: d.last_signal,
              demo_mode: d.demo_mode,
              indicators: d.indicators,
              position_size: d.position_size,
              risk: d.risk,
              error_count: brokerState.error_count ?? 0,
              last_update: brokerState.last_update ?? null,
              ...(d.key_invalid != null ? { key_invalid: d.key_invalid } : {}),
            }
            return { ...prev, [broker]: updatedBroker, running: prev.running || true }
          })
        } else if (d.type === 'trade_opened') {
          if (d.demo_balance != null) {
            if (d.balance_broker === 'capital') setCapitalDemoBalance(d.demo_balance)
            else setLiveDemoBalance(d.demo_balance)
          }
          addFeed('trade_opened', `${String(d.side || 'UNKNOWN').toUpperCase()} ${d.symbol} @ $${typeof d.entry_price === 'number' ? d.entry_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : 'N/A'}${d.demo_mode ? ' [DEMO]' : ''}`, 'text-profit')
          loadData()
        } else if (d.type === 'trade_closed') {
          const pnl = (typeof d.pnl === 'number' && !isNaN(d.pnl)) ? d.pnl : 0
          if (d.demo_balance != null) {
            if (d.balance_broker === 'capital') setCapitalDemoBalance(d.demo_balance)
            else setLiveDemoBalance(d.demo_balance)
          }
          if (typeof d.daily_pnl === 'number') setDailyPnl(d.daily_pnl)
          if (typeof d.daily_target === 'number') setDailyTarget(d.daily_target)
          if (typeof d.daily_progress_pct === 'number') setDailyProgressPct(d.daily_progress_pct)
          const targetMsg = d.daily_target_hit ? ' 🎯 Daily target hit — still hunting more profit (lighter size)' : ` | Day: ${d.daily_progress_pct?.toFixed(0) ?? 0}% of $${d.daily_target?.toFixed(0) ?? 200}`
          addFeed('trade_closed', `Closed (${d.exit_reason}) P&L: ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}${d.demo_mode ? ' [DEMO]' : ''}${targetMsg}`, pnl >= 0 ? 'text-profit' : 'text-loss')
          loadData()
        } else if (d.type === 'ai_analysis_ready') {
          addFeed('ai', `AI graded last trade: ${d.analysis?.grade ?? 'N/A'}`, 'text-purple-400')
          loadData()
        } else if (d.type === 'bot_error') {
          addFeed('error', `Error: ${d.message}`, 'text-loss')
        } else if (d.type === 'key_invalid') {
          addFeed('error', d.message, 'text-loss')
          setBotStatus(prev => prev ? { ...prev, key_invalid: true } : null)
          loadData()
        } else if (d.type === 'risk_pause') {
          addFeed('error', `Risk Manager: ${d.message}`, 'text-warning')
          loadData()
        } else if (d.type === 'calibration_applied') {
          addFeed('ai', `Pro: Strategy calibrated — ${d.summary}`, 'text-purple-400')
          loadData()
        } else if (d.type === 'premium_activated') {
          setIsPremium(true)
          addFeed('ai', 'Nalo.Ai Pro activated!', 'text-purple-400')
          loadData()
        } else if (d.type === 'payment_failed') {
          addFeed('error', d.message, 'text-loss')
        }
      } catch (err) { console.error('WebSocket message error:', err) }
    }

    ws.onclose = (ev) => {
      if (ev.code === 4002) {
        if (reconnectRef.current) clearTimeout(reconnectRef.current)
        if (wsAuthFailedRef.current) return
        wsAuthFailedRef.current = true
        api.post('/api/auth/refresh').then(() => {
          wsAuthFailedRef.current = false
          reconnectRef.current = setTimeout(connectWS, 500)
        }).catch(async () => {
          await logout()
          navigate('/')
        })
        return
      }
      if (!wsAuthFailedRef.current) {
        reconnectRef.current = setTimeout(connectWS, 3000)
      }
    }
    ws.onerror = () => ws.close()
  }, [addFeed, loadData])

  // Market price poll
  useEffect(() => {
    const symbol = settings?.trading_symbol ?? 'BTC-USD'
    primarySymbolRef.current = symbol
    let stale = false
    const fetchPrice = async () => {
      // Skip market price fetch when bot is running — bot sends its own price via WebSocket
      // This prevents mixing real market prices with simulated demo prices
      if (botStatus?.running || botStatus?.robinhood?.running || botStatus?.capital?.running) return
      try {
        const res = await api.get(`/api/market/price?symbol=${symbol}`)
        if (res.data.price && !stale) setLivePrice(res.data.price)
      } catch {}
    }
    fetchPrice()
    const interval = setInterval(fetchPrice, 5000)
    return () => { stale = true; clearInterval(interval) }
  }, [settings?.trading_symbol, botStatus?.running, botStatus?.robinhood?.running, botStatus?.capital?.running])

  useEffect(() => {
    loadData()
    const t = setTimeout(connectWS, 400)
    // Check for Stripe redirect
    const params = new URLSearchParams(window.location.search)
    if (params.get('premium') === 'success') {
      showToast('Nalo.Ai Pro activated! AI auto-calibration is now live.')
      window.history.replaceState({}, '', '/dashboard')
    } else if (params.get('premium') === 'cancelled') {
      showToast('Checkout cancelled', false)
      window.history.replaceState({}, '', '/dashboard')
    }
    return () => {
      clearTimeout(t)
      if (reconnectRef.current) clearTimeout(reconnectRef.current)
      wsRef.current?.close()
    }
  }, [loadData, connectWS])

  const startBot = async (broker: 'robinhood' | 'capital', mode: 'demo' | 'live') => {
    const setLoading = broker === 'capital' ? setBotLoadingCap : setBotLoadingRh
    setLoading(true)
    setBotLoading(true)
    try {
      await api.post('/api/bot/start', { mode, broker })
      await refreshUser()
      const r = await api.get('/api/bot/status')
      setBotStatus(r.data as BotStatusMap)
      const label = broker === 'capital' ? 'Capital.com' : 'Robinhood'
      showToast(`${label} bot started (${mode})`)
      setTradeFilter(mode === 'live' ? 'live' : 'demo')
      addFeed('connected', `${label} bot running in ${mode.toUpperCase()} mode`, mode === 'live' ? 'text-profit' : 'text-warning')
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      showToast(detail ?? 'Failed to start bot', false)
    } finally { setLoading(false); setBotLoading(false) }
  }

  const stopBot = async (broker: 'robinhood' | 'capital' | 'all' = 'all') => {
    const setLoading = broker === 'capital' ? setBotLoadingCap : broker === 'robinhood' ? setBotLoadingRh : setBotLoading
    setLoading(true)
    setBotLoading(true)
    try {
      await api.post('/api/bot/stop', { broker })
      await refreshUser()
      const r = await api.get('/api/bot/status')
      setBotStatus(r.data as BotStatusMap)
      const label = broker === 'capital' ? 'Capital.com' : broker === 'robinhood' ? 'Robinhood' : 'All bots'
      showToast(`${label} stopped`)
    } catch { showToast('Failed to stop bot', false) }
    finally { setLoading(false); setBotLoading(false) }
  }

  const saveKeys = async () => {
    if (!rhApiKey) return showToast('API key is required', false)
    setKeysLoading(true)
    try {
      const res = await api.post('/api/bot/keys', { rh_api_key: rhApiKey })
      await refreshUser()
      await loadData()
      api.get('/api/bot/balance').then(r => setBalance(r.data)).catch(() => {})
      showToast(res.data?.message?.includes('live') ? 'Switched to LIVE mode!' : 'API key saved!')
      setRhApiKey('')
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      showToast(detail ?? 'Failed to save keys', false)
    } finally { setKeysLoading(false) }
  }

  const requestSaveSettings = () => {
    setPendingSettings({
      trading_symbol: formSymbol,
      // Strategy internals — always optimal defaults, hidden from UI
      entry_z: 2.0, lookback: 20,
      use_rsi_filter: true, use_ema_filter: true, use_adx_filter: true,
      use_bbands_filter: true, use_macd_filter: false, use_volume_filter: false,
      // User-configurable
      stop_loss_pct: formStopLoss, take_profit_pct: formTakeProfit, trail_stop_pct: formTrailStop,
      max_drawdown_pct: formMaxDrawdown, max_stops_before_pause: formMaxStops,
      cooldown_ticks: formCooldown, risk_per_trade_pct: formRiskPerTrade, max_exposure_pct: formMaxExposure,
      position_size_mode: formPosMode, fixed_quantity: formFixedQty,
      telegram_enabled: formTelegramEnabled,
      broker_type: formBroker,
    })
    setShowConfirm(true)
  }

  const confirmSaveSettings = async () => {
    if (!pendingSettings) return
    setShowConfirm(false)
    setSettingsLoading(true)
    try {
      await api.post('/api/bot/settings', { ...pendingSettings, exit_z: 0.5 })
      showToast('Settings saved!')
      await loadData()
    } catch { showToast('Failed to save settings', false) }
    finally { setSettingsLoading(false); setPendingSettings(null) }
  }


  const testConnection = async () => {
    setTestLoading(true); setTestResult(null)
    try {
      const res = await api.post('/api/bot/test-connection')
      if (res.data.ok) {
        setTestResult({ ok: true, msg: `Connected! Buying power: $${res.data.buying_power?.toFixed(2) ?? '0.00'}` })
        setBotStatus(prev => prev ? { ...prev, key_invalid: false } : null)
        loadData()
      } else { setTestResult({ ok: false, msg: res.data.error ?? 'Connection failed' }) }
    } catch { setTestResult({ ok: false, msg: 'Request failed' }) }
    finally { setTestLoading(false) }
  }

  const saveCapitalKeys = async () => {
    if (!capitalApiKey || !capitalIdentifier || !capitalPassword) return showToast('All Capital.com fields required', false)
    setCapitalKeyLoading(true); setCapitalTestResult(null)
    try {
      await api.post('/api/bot/capital-keys', { capital_api_key: capitalApiKey, capital_identifier: capitalIdentifier, capital_password: capitalPassword })
      showToast('Capital.com credentials saved!')
      setFormBroker('capital')
      await loadData()
    } catch { showToast('Failed to save Capital.com credentials', false) }
    finally { setCapitalKeyLoading(false) }
  }

  const testCapitalConnection = async () => {
    setCapitalTestLoading(true); setCapitalTestResult(null)
    try {
      const res = await api.post('/api/bot/test-capital-connection')
      setCapitalTestResult({ ok: res.data.ok, msg: res.data.message ?? res.data.error ?? '' })
    } catch { setCapitalTestResult({ ok: false, msg: 'Request failed' }) }
    finally { setCapitalTestLoading(false) }
  }

  const saveTradovateKeys = async () => {
    if (!tradovateUsername || !tradovatePassword || !tradovateAccountId) return showToast('All Tradovate fields required', false)
    setTradovateKeyLoading(true); setTradovateTestResult(null)
    try {
      await api.post('/api/bot/tradovate-keys', { tradovate_username: tradovateUsername, tradovate_password: tradovatePassword, tradovate_account_id: parseInt(tradovateAccountId) })
      showToast('Tradovate credentials saved!')
      setFormBroker('tradovate')
      await loadData()
    } catch { showToast('Failed to save Tradovate credentials', false) }
    finally { setTradovateKeyLoading(false) }
  }

  const testTradovateConnection = async () => {
    setTradovateTestLoading(true); setTradovateTestResult(null)
    try {
      const res = await api.post('/api/bot/test-tradovate-connection')
      setTradovateTestResult({ ok: res.data.ok, msg: res.data.message ?? res.data.error ?? '' })
    } catch { setTradovateTestResult({ ok: false, msg: 'Request failed' }) }
    finally { setTradovateTestLoading(false) }
  }

  const saveTelegram = async () => {
    if (!telegramToken || !telegramChatId) return showToast('Both fields required', false)
    setTelegramLoading(true)
    try {
      await api.post('/api/bot/telegram', { bot_token: telegramToken, chat_id: telegramChatId })
      showToast('Telegram configured!')
      setTelegramToken(''); setTelegramChatId('')
      await loadData()
    } catch { showToast('Failed to save Telegram config', false) }
    finally { setTelegramLoading(false) }
  }

  const testTelegram = async () => {
    setTelegramLoading(true)
    try {
      const res = await api.post('/api/bot/telegram/test')
      showToast(res.data.ok ? 'Test message sent!' : res.data.error, res.data.ok)
    } catch { showToast('Test failed', false) }
    finally { setTelegramLoading(false) }
  }


  const activatePremium = async () => {
    setPremiumLoading(true)
    try {
      const res = await api.post('/api/stripe/create-checkout')
      if (res.data.checkout_url) {
        window.location.href = res.data.checkout_url
      } else {
        showToast('Failed to create checkout session', false)
      }
    } catch {
      showToast('Payment system unavailable. Please try again.', false)
    }
    finally { setPremiumLoading(false) }
  }

  const deactivatePremium = async () => {
    if (!confirm('Are you sure you want to cancel Nalo.Ai Pro? You\'ll keep access until the end of your billing period.')) return
    setPremiumLoading(true)
    try {
      const res = await api.post('/api/stripe/cancel')
      if (res.data.is_premium === false) {
        setIsPremium(false)
        showToast('Premium deactivated')
      } else {
        showToast(res.data.message || 'Subscription will cancel at end of billing period')
      }
      loadData()
    } catch { showToast('Failed to cancel', false) }
    finally { setPremiumLoading(false) }
  }

  const manageSubscription = async () => {
    try {
      const res = await api.get('/api/stripe/portal')
      if (res.data.portal_url) window.location.href = res.data.portal_url
      else showToast('Billing portal not available yet', false)
    } catch { showToast('Billing portal not configured yet. Contact support.', false) }
  }

  const resumeTrading = async () => {
    try {
      await api.post('/api/bot/risk/resume')
      showToast('Trading resumed')
      loadData()
    } catch { showToast('Failed to resume', false) }
  }

  const handleLogout = async () => { await logout(); navigate('/') }
  const isDemo = settings?.demo_mode ?? !user?.has_api_keys
  // Per-broker running status helpers
  const rhStatus = botStatus?.robinhood ?? null
  const capStatus = botStatus?.capital ?? null
  const anyBotRunning = (botStatus?.running) || rhStatus?.running || capStatus?.running || false
  // Sanitize bot signals — hide strategy details from users
  const sanitizeSignal = (signal: string | null | undefined): string => {
    if (!signal) return ''
    if (signal.startsWith('Warming up')) return 'Analyzing market data...'
    if (signal.startsWith('Paused:')) return signal  // Risk pause is OK to show
    if (signal.includes('Signal filtered')) return 'Waiting for optimal entry...'
    if (signal.includes('Bullish retest') || signal.includes('Bearish retest')) return 'Signal detected'
    if (signal.includes('Z=')) return 'Monitoring market conditions...'
    return 'Scanning for opportunities...'
  }

  // Use robinhood primary status for the live price PnL display (RH trades BTC etc.)
  const _primaryStatus = rhStatus?.in_trade ? rhStatus : capStatus
  const currentPnl = _primaryStatus?.in_trade && _primaryStatus.entry_price && _primaryStatus.entry_price > 0 && livePrice
    ? _primaryStatus.trade_side === 'buy'
      ? ((livePrice - _primaryStatus.entry_price) / _primaryStatus.entry_price) * 100
      : ((_primaryStatus.entry_price - livePrice) / _primaryStatus.entry_price) * 100
    : null

  // ── Render ──────────────────────────────────────────────────────────────────

  // Prefer per-broker /balances response (which fetches real broker cash in live mode).
  // Fall back to WebSocket-updated demo values, then settings, then 10k default.
  // Without this fix, live-mode users saw demo_balance relabeled as "Live Balance".
  const rhBalance = brokerBalances?.robinhood?.available
    ?? liveDemoBalance
    ?? settings?.demo_balance
    ?? 10000
  const capBalance = brokerBalances?.capital?.available
    ?? capitalDemoBalance
    ?? settings?.capital_demo_balance
    ?? 10000
  const totalBalance = rhBalance + capBalance

  return (
    <div className="min-h-screen bg-dark text-white flex flex-col">
      {toast && <Toast msg={toast.msg} ok={toast.ok} />}

      {/* ── Header ── */}
      <header className="sticky top-0 z-40 border-b border-border bg-dark/95 backdrop-blur-md">
        <div className="max-w-5xl mx-auto px-4 h-14 flex items-center justify-between gap-3">
          {/* Logo */}
          <div className="flex items-center gap-2">
            <span className="text-lg">⚡</span>
            <span className="font-bold text-base tracking-tight">Nalo.Ai</span>
            {isPremium && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-400 border border-purple-500/30 font-semibold">PRO</span>
            )}
          </div>

          {/* Live price pill */}
          <div className="flex items-center gap-2">
            {anyBotRunning && (
              <div className="flex items-center gap-1.5">
                <div className="w-1.5 h-1.5 rounded-full bg-profit animate-pulse" />
                <span className="text-xs text-muted font-mono">{settings?.trading_symbol ?? 'BTC-USD'}</span>
              </div>
            )}
            {livePrice ? (
              <span className="font-mono font-bold text-sm">${livePrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
            ) : null}
          </div>

          {/* Right side */}
          <div className="flex items-center gap-2">
            {user?.is_admin && (
              <button onClick={() => navigate('/admin')} className="text-xs px-3 py-1.5 rounded-lg bg-purple-500/15 border border-purple-500/30 text-purple-400 font-semibold hidden sm:block">Admin</button>
            )}
            <button onClick={handleLogout} className="text-xs px-3 py-1.5 rounded-lg border border-border text-muted hover:text-white transition-colors">Logout</button>
          </div>
        </div>
      </header>

      {/* ── Tab nav ── */}
      <div className="max-w-5xl mx-auto px-4 w-full">
        <div className="flex gap-1 pt-4 pb-3 border-b border-border/50">
          {([
            { id: 'dashboard', label: '🏠 Home' },
            { id: 'trades',    label: '📋 Trades' },
            { id: 'settings',  label: '⚙️ Settings' },
          ] as const).map(tab => (
            <button key={tab.id} onClick={() => setActiveTab(tab.id as any)}
              className={`px-5 py-2 rounded-lg text-sm font-semibold transition-colors ${
                activeTab === tab.id
                  ? 'bg-accent/15 text-accent border border-accent/25'
                  : 'text-muted hover:text-white'
              }`}>
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      <main className="max-w-5xl mx-auto px-4 w-full py-6 space-y-5 flex-1">

        {/* ════════════════════════ HOME TAB ════════════════════════ */}
        {activeTab === 'dashboard' && (
          <>
            {/* Alerts */}
            {rhStatus?.key_invalid && (
              <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-loss/10 border border-loss/30">
                <span className="text-loss">⚠️</span>
                <p className="text-sm text-loss flex-1">Robinhood API key invalid — update in Settings.</p>
                <button onClick={() => setActiveTab('settings')} className="text-xs px-3 py-1.5 rounded-lg bg-loss/20 border border-loss/30 text-loss font-medium">Fix →</button>
              </div>
            )}
            {_primaryStatus?.risk?.is_paused && (
              <div className="flex items-center gap-3 px-4 py-3 rounded-xl bg-warning/10 border border-warning/30">
                <span>⏸️</span>
                <p className="text-sm text-warning flex-1">Trading paused — {_primaryStatus.risk.pause_reason}</p>
                <button onClick={resumeTrading} className="text-xs px-3 py-1.5 rounded-lg bg-accent/20 border border-accent/30 text-accent font-medium">Resume</button>
              </div>
            )}

            {/* ── Hero: Balance + Stats ── */}
            <div className="rounded-2xl border border-border bg-card p-6">
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
                {/* Total balance */}
                <div className="sm:col-span-1">
                  <p className="text-xs text-muted mb-1 uppercase tracking-wider">Total Portfolio</p>
                  <p className="text-4xl font-bold font-mono text-white">
                    ${totalBalance.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </p>
                  <div className="flex items-center gap-2 mt-2">
                    {anyBotRunning
                      ? (rhStatus?.demo_mode !== false || capStatus?.demo_mode !== false)
                        ? <span className="text-xs px-2 py-0.5 rounded-full bg-warning/15 text-warning border border-warning/30 font-medium">DEMO MODE</span>
                        : <span className="text-xs px-2 py-0.5 rounded-full bg-profit/15 text-profit border border-profit/30 font-medium">LIVE MODE</span>
                      : <span className="text-xs text-muted">Bot not running</span>
                    }
                  </div>
                </div>

                {/* Key stats */}
                <div className="sm:col-span-2 grid grid-cols-3 gap-4">
                  <div className="p-3 rounded-xl bg-elevated text-center">
                    <p className="text-xs text-muted mb-1">Today's P&amp;L</p>
                    <p className={`text-xl font-bold font-mono ${dailyPnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                      {dailyPnl >= 0 ? '+' : ''}${dailyPnl.toFixed(2)}
                    </p>
                  </div>
                  <div className="p-3 rounded-xl bg-elevated text-center">
                    <p className="text-xs text-muted mb-1">Win Rate</p>
                    <p className={`text-xl font-bold font-mono ${(stats?.win_rate ?? 0) >= 50 ? 'text-profit' : 'text-loss'}`}>
                      {stats?.win_rate ?? 0}%
                    </p>
                  </div>
                  <div className="p-3 rounded-xl bg-elevated text-center">
                    <p className="text-xs text-muted mb-1">Total Trades</p>
                    <p className="text-xl font-bold font-mono text-white">{stats?.total ?? 0}</p>
                  </div>
                </div>
              </div>

              {/* Daily target bar */}
              {anyBotRunning && (
                <div className="mt-5 pt-4 border-t border-border/50">
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-xs text-muted">Daily Goal</p>
                    <p className={`text-xs font-mono font-bold ${dailyPnl >= dailyTarget ? 'text-profit' : 'text-muted'}`}>
                      ${dailyPnl.toFixed(0)} / ${dailyTarget.toFixed(0)}
                      {dailyPnl >= dailyTarget && ' 🎯'}
                    </p>
                  </div>
                  <div className="h-1.5 rounded-full bg-elevated overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all duration-700 ${dailyPnl >= dailyTarget ? 'bg-profit' : 'bg-accent'}`}
                      style={{ width: `${Math.min(100, Math.max(0, dailyProgressPct))}%` }}
                    />
                  </div>
                </div>
              )}
            </div>

            {/* ── Broker Breakdown ──
                Per-broker P&L tracking — each broker has independent server-side
                accounting (separate risk_manager, separate demo balance, separate
                stats endpoint). Today's P&L comes from the risk_manager's daily_pnl
                (resets at UTC midnight via RiskManager.reset_daily). All-time numbers
                come from the trades table filtered by broker symbol set. */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {/* Robinhood Crypto card */}
              <div className="p-4 rounded-2xl border border-border bg-card">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <span className="text-lg">🪙</span>
                    <p className="text-sm font-semibold">Robinhood Crypto</p>
                  </div>
                  <span className="text-xs text-muted">BTC · ETH · SOL · DOGE</span>
                </div>
                <p className="text-2xl font-bold font-mono mb-3">
                  ${rhBalance.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </p>
                <div className="grid grid-cols-3 gap-2 pt-3 border-t border-border/50">
                  <div>
                    <p className="text-[10px] text-muted uppercase mb-1">Today</p>
                    <p className={`text-sm font-mono font-bold ${(rhStatus?.risk?.daily_pnl ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                      {(rhStatus?.risk?.daily_pnl ?? 0) >= 0 ? '+' : ''}${(rhStatus?.risk?.daily_pnl ?? 0).toFixed(2)}
                    </p>
                  </div>
                  <div>
                    <p className="text-[10px] text-muted uppercase mb-1">All-Time</p>
                    <p className={`text-sm font-mono font-bold ${(rhStats?.total_pnl ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                      {(rhStats?.total_pnl ?? 0) >= 0 ? '+' : ''}${(rhStats?.total_pnl ?? 0).toFixed(2)}
                    </p>
                  </div>
                  <div>
                    <p className="text-[10px] text-muted uppercase mb-1">Trades</p>
                    <p className="text-sm font-mono font-bold text-white">
                      {rhStats?.total ?? 0} <span className="text-muted text-xs">· {rhStats?.win_rate ?? 0}%</span>
                    </p>
                  </div>
                </div>
              </div>

              {/* Capital.com card */}
              <div className="p-4 rounded-2xl border border-border bg-card">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <span className="text-lg">📈</span>
                    <p className="text-sm font-semibold">Capital.com CFDs</p>
                  </div>
                  <span className="text-xs text-muted">GOLD · US100</span>
                </div>
                <p className="text-2xl font-bold font-mono mb-3">
                  ${capBalance.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </p>
                <div className="grid grid-cols-3 gap-2 pt-3 border-t border-border/50">
                  <div>
                    <p className="text-[10px] text-muted uppercase mb-1">Today</p>
                    <p className={`text-sm font-mono font-bold ${(capStatus?.risk?.daily_pnl ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                      {(capStatus?.risk?.daily_pnl ?? 0) >= 0 ? '+' : ''}${(capStatus?.risk?.daily_pnl ?? 0).toFixed(2)}
                    </p>
                  </div>
                  <div>
                    <p className="text-[10px] text-muted uppercase mb-1">All-Time</p>
                    <p className={`text-sm font-mono font-bold ${(capStats?.total_pnl ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                      {(capStats?.total_pnl ?? 0) >= 0 ? '+' : ''}${(capStats?.total_pnl ?? 0).toFixed(2)}
                    </p>
                  </div>
                  <div>
                    <p className="text-[10px] text-muted uppercase mb-1">Trades</p>
                    <p className="text-sm font-mono font-bold text-white">
                      {capStats?.total ?? 0} <span className="text-muted text-xs">· {capStats?.win_rate ?? 0}%</span>
                    </p>
                  </div>
                </div>
              </div>
            </div>

            {/* ── Active Trade Banner ── */}
            {_primaryStatus?.in_trade && _primaryStatus.entry_price && (
              <div className="px-5 py-4 rounded-xl border bg-elevated border-accent/30 flex flex-wrap items-center gap-5">
                <div className="flex items-center gap-2">
                  <div className="w-2 h-2 rounded-full bg-profit animate-pulse" />
                  <span className="text-sm font-semibold">Trade Open</span>
                </div>
                <div>
                  <p className="text-xs text-muted">Side</p>
                  <span className={`text-sm font-bold ${_primaryStatus.trade_side === 'buy' ? 'text-profit' : 'text-loss'}`}>
                    {(_primaryStatus.trade_side ?? '').toUpperCase()}
                  </span>
                </div>
                <div>
                  <p className="text-xs text-muted">Entry</p>
                  <p className="text-sm font-mono font-bold">${_primaryStatus.entry_price.toLocaleString('en-US', { minimumFractionDigits: 2 })}</p>
                </div>
                {livePrice && (
                  <div>
                    <p className="text-xs text-muted">Now</p>
                    <p className="text-sm font-mono font-bold">${livePrice.toLocaleString('en-US', { minimumFractionDigits: 2 })}</p>
                  </div>
                )}
                {currentPnl !== null && (
                  <div>
                    <p className="text-xs text-muted">Unrealised</p>
                    <p className={`text-sm font-mono font-bold ${currentPnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                      {currentPnl >= 0 ? '+' : ''}{currentPnl.toFixed(2)}%
                    </p>
                  </div>
                )}
                {_primaryStatus.trail_stop && (
                  <div>
                    <p className="text-xs text-muted">Trail Stop</p>
                    <p className="text-sm font-mono text-warning">${_primaryStatus.trail_stop.toFixed(2)}</p>
                  </div>
                )}
              </div>
            )}

            {/* ── AI Signal Agent toggle ──
                When enabled, every signal that passed the hardcoded filters is
                then screened by a Claude agent that reads market context via
                tools and decides enter/skip with reasoning. Logs go to last_signal
                so you can read each decision live. */}
            <div className={`flex items-center justify-between gap-3 px-4 py-3 rounded-xl border ${
              user?.use_ai_signal_agent
                ? 'border-purple-500/40 bg-purple-500/10'
                : 'border-border bg-card'
            }`}>
              <div>
                <p className="text-sm font-semibold">
                  {user?.use_ai_signal_agent ? '🧠 AI Signal Agent: ON' : '🧠 AI Signal Agent: OFF'}
                </p>
                <p className="text-xs text-muted mt-0.5">
                  {user?.use_ai_signal_agent
                    ? 'Every entry signal is screened by Claude (~$0.02/signal). Watch last_signal for decisions.'
                    : 'Hardcoded filters only. Enable to let Claude reason about each entry.'}
                </p>
              </div>
              <button
                onClick={async () => {
                  const next = !user?.use_ai_signal_agent
                  try {
                    const r = await api.post('/api/bot/ai-signal-agent', { enabled: next })
                    setToast({ msg: r.data.message, ok: true })
                    await refreshUser()
                  } catch (e: any) {
                    setToast({ msg: e?.response?.data?.detail || 'Failed', ok: false })
                  }
                }}
                className={`text-xs px-3 py-1.5 rounded-lg font-semibold transition-colors ${
                  user?.use_ai_signal_agent
                    ? 'bg-elevated border border-border text-white hover:bg-card'
                    : 'bg-purple-500/20 border border-purple-500/40 text-purple-300 hover:bg-purple-500/30'
                }`}
              >
                {user?.use_ai_signal_agent ? 'Turn OFF' : 'Turn ON'}
              </button>
            </div>

            {/* ── Force Demo Robinhood toggle ── */}
            {/* Lets the user verify the trading pipeline end-to-end with synthetic
                signals (~2/min) without risking real Robinhood cash. Capital.com
                is unaffected — it already uses the demo Capital.com API. */}
            {settings?.has_api_keys && (
              <div className={`flex items-center justify-between gap-3 px-4 py-3 rounded-xl border ${
                user?.force_demo_robinhood
                  ? 'border-yellow-500/40 bg-yellow-500/10'
                  : 'border-border bg-card'
              }`}>
                <div>
                  <p className="text-sm font-semibold">
                    {user?.force_demo_robinhood ? '🧪 Robinhood: DEMO mode (forced)' : '💰 Robinhood: LIVE mode'}
                  </p>
                  <p className="text-xs text-muted mt-0.5">
                    {user?.force_demo_robinhood
                      ? 'Synthetic signals firing ~2/min — verifying pipeline.'
                      : 'Real Robinhood account in use.'}
                  </p>
                </div>
                <button
                  onClick={async () => {
                    const next = !user?.force_demo_robinhood
                    try {
                      const r = await api.post('/api/bot/force-demo-robinhood', { enabled: next })
                      setToast({ msg: r.data.message, ok: true })
                      await refreshUser()
                    } catch (e: any) {
                      setToast({ msg: e?.response?.data?.detail || 'Failed', ok: false })
                    }
                  }}
                  className={`text-xs px-3 py-1.5 rounded-lg font-semibold transition-colors ${
                    user?.force_demo_robinhood
                      ? 'bg-elevated border border-border text-white hover:bg-card'
                      : 'bg-yellow-500/20 border border-yellow-500/40 text-yellow-300 hover:bg-yellow-500/30'
                  }`}
                >
                  {user?.force_demo_robinhood ? 'Switch to LIVE' : 'Switch to DEMO'}
                </button>
              </div>
            )}

            {/* ── Bot Controls ── */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <BotCard
                broker="robinhood" label="Robinhood Crypto" icon="🪙"
                symbols={['BTC', 'ETH', 'SOL', 'DOGE']}
                status={rhStatus} loading={botLoadingRh}
                hasKeys={settings?.has_api_keys ?? false}
                onStart={mode => startBot('robinhood', mode)} onStop={() => stopBot('robinhood')}
                balance={rhBalance} balanceLabel={rhStatus?.demo_mode !== false ? 'Demo Balance' : 'Live Balance'}
              />
              <BotCard
                broker="capital" label="Capital.com CFDs" icon="📈"
                symbols={['GOLD', 'US100']}
                status={capStatus} loading={botLoadingCap}
                hasKeys={settings?.has_capital_keys ?? false}
                onStart={mode => startBot('capital', mode)} onStop={() => stopBot('capital')}
                balance={capBalance} balanceLabel={capStatus?.demo_mode !== false ? 'Demo Balance' : 'Live Balance'}
              />
            </div>

            {/* ── Price Chart ── */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-sm font-semibold">Price Chart</h2>
                {livePrice && <span className="font-mono text-sm text-muted">${livePrice.toLocaleString('en-US', { minimumFractionDigits: 2 })}</span>}
              </div>
              {priceHistory.length > 5 ? (
                <ResponsiveContainer width="100%" height={200}>
                  <ComposedChart data={priceHistory}>
                    <defs>
                      <linearGradient id="priceGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#6366F1" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#6366F1" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <XAxis dataKey="time" tick={{ fontSize: 9, fill: '#A0A3B1' }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
                    <YAxis tick={{ fontSize: 10, fill: '#A0A3B1' }} axisLine={false} tickLine={false} width={72} domain={['auto', 'auto']}
                      tickFormatter={(v: number) => `$${v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(0)}`} />
                    <Tooltip contentStyle={{ backgroundColor: '#1A1B23', border: '1px solid #2A2B35', borderRadius: 8, fontSize: 12 }} />
                    <Area type="monotone" dataKey="price" stroke="#6366F1" fill="url(#priceGrad)" strokeWidth={2} dot={false} />
                    {_primaryStatus?.entry_price && <ReferenceLine y={_primaryStatus.entry_price} stroke="#10B981" strokeDasharray="3 3" />}
                    {_primaryStatus?.trail_stop && <ReferenceLine y={_primaryStatus.trail_stop} stroke="#EF4444" strokeDasharray="3 3" />}
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-48 flex flex-col items-center justify-center gap-2 text-muted">
                  <span className="text-3xl">📊</span>
                  <p className="text-sm">Start the bot to see live price data</p>
                </div>
              )}
            </div>

            {/* ── Equity + Feed ── */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
              {/* Equity Curve */}
              <div className="bg-card border border-border rounded-2xl p-5">
                <h2 className="text-sm font-semibold mb-4">Portfolio Growth</h2>
                {equityCurve.length > 1 ? (
                  <ResponsiveContainer width="100%" height={150}>
                    <AreaChart data={equityCurve}>
                      <defs>
                        <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#10B981" stopOpacity={0.3} />
                          <stop offset="95%" stopColor="#10B981" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <XAxis dataKey="time" tick={{ fontSize: 9, fill: '#A0A3B1' }} axisLine={false} tickLine={false} />
                      <YAxis tick={{ fontSize: 10, fill: '#A0A3B1' }} axisLine={false} tickLine={false} width={68} domain={['auto', 'auto']}
                        tickFormatter={(v: number) => `$${v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(0)}`} />
                      <Tooltip contentStyle={{ backgroundColor: '#1A1B23', border: '1px solid #2A2B35', borderRadius: 8, fontSize: 12 }}
                        formatter={(v: number) => [`$${v.toFixed(2)}`, 'Balance']} />
                      <Area type="monotone" dataKey="balance" stroke="#10B981" fill="url(#eqGrad)" strokeWidth={2} dot={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="h-36 flex items-center justify-center text-muted text-sm">Equity curve builds after your first trades.</div>
                )}
              </div>

              {/* Live Feed */}
              <div className="bg-card border border-border rounded-2xl p-5">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-sm font-semibold">Live Activity</h2>
                  <div className="flex items-center gap-1.5">
                    <div className="w-1.5 h-1.5 rounded-full bg-profit animate-pulse" />
                    <span className="text-xs text-muted">Live</span>
                  </div>
                </div>
                <div className="space-y-2 max-h-52 overflow-y-auto">
                  {feed.length === 0 ? (
                    <p className="text-muted text-sm text-center py-8">Bot activity will appear here.</p>
                  ) : feed.map(ev => (
                    <div key={ev.id} className="flex items-start gap-2.5 p-2.5 rounded-xl bg-elevated border border-border/40">
                      <span className={`text-sm flex-shrink-0 ${ev.color}`}>
                        {ev.type === 'trade_opened' ? '📈' : ev.type === 'trade_closed' ? '📉' : ev.type === 'ai' ? '🧠' : ev.type === 'error' ? '⚠️' : ev.type === 'connected' ? '🔌' : '·'}
                      </span>
                      <div className="flex-1 min-w-0">
                        <p className={`text-xs leading-relaxed ${ev.color} break-words`}>{ev.message}</p>
                        <p className="text-xs text-muted/50 mt-0.5">{ev.time}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* ── AI Report ── */}
            {report && (
              <div className="bg-card border border-border rounded-2xl p-5">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-sm font-semibold">🧠 AI Daily Report</h2>
                  <span className="text-xs text-muted">{report.report_date}</span>
                </div>
                <div className="grid grid-cols-3 gap-3 mb-4">
                  {[
                    { label: 'Win Rate', val: `${report.win_rate.toFixed(1)}%`, color: report.win_rate >= 50 ? 'text-profit' : 'text-loss' },
                    { label: 'Trades', val: String(report.total_trades), color: 'text-white' },
                    { label: 'P&L', val: `${report.total_pnl >= 0 ? '+' : ''}$${report.total_pnl.toFixed(2)}`, color: report.total_pnl >= 0 ? 'text-profit' : 'text-loss' },
                  ].map((s, i) => (
                    <div key={i} className="p-3 rounded-xl bg-elevated text-center">
                      <p className="text-xs text-muted mb-1">{s.label}</p>
                      <p className={`font-bold font-mono text-sm ${s.color}`}>{s.val}</p>
                    </div>
                  ))}
                </div>
                {report.summary && <p className="text-sm text-muted leading-relaxed">{report.summary}</p>}
                {report.top_improvement && (
                  <div className="mt-3 px-4 py-2.5 rounded-xl bg-warning/10 border border-warning/20">
                    <p className="text-xs text-warning font-semibold mb-1">Tip</p>
                    <p className="text-sm">{report.top_improvement}</p>
                  </div>
                )}
              </div>
            )}

            {/* ── Pro Upsell ── */}
            {!isPremium && (
              <div className="rounded-2xl border border-purple-500/30 bg-gradient-to-r from-purple-900/20 to-accent/5 p-6">
                <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-xl">🧠</span>
                      <h3 className="font-bold">Nalo.Ai Pro</h3>
                      <span className="text-xs px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-400 border border-purple-500/30 font-semibold">$199/mo</span>
                    </div>
                    <p className="text-sm text-muted">AI analyses every trade and auto-tunes your strategy parameters to improve profitability over time.</p>
                  </div>
                  <button onClick={() => setShowPremiumModal(true)}
                    className="px-6 py-2.5 rounded-xl bg-purple-600 hover:bg-purple-500 font-semibold text-sm transition-colors shadow-lg shadow-purple-600/20 whitespace-nowrap">
                    Upgrade to Pro
                  </button>
                </div>
              </div>
            )}

            {isPremium && (
              <div className="rounded-2xl border border-purple-500/30 bg-purple-900/10 p-5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span>🧠</span>
                    <span className="font-semibold text-sm">Nalo.Ai Pro Active</span>
                    <span className="text-xs px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-400 border border-purple-500/30">AI Calibration ON</span>
                  </div>
                  <button onClick={manageSubscription} className="text-xs px-3 py-1.5 rounded-lg border border-purple-500/30 text-purple-400 hover:bg-purple-500/10 transition-colors">Manage</button>
                </div>
                {calibrations.length > 0 && (
                  <p className="text-xs text-muted mt-2">{calibrations.length} calibrations applied — your bot is getting smarter.</p>
                )}
              </div>
            )}
          </>
        )}

        {/* ════════════════════════ TRADES TAB ════════════════════════ */}
        {activeTab === 'trades' && (
          <>
            {/* Filters */}
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex gap-1 bg-elevated rounded-xl p-1">
                {(['all', 'demo', 'live'] as const).map(m => (
                  <button key={m} onClick={() => setTradeFilter(m)}
                    className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors ${
                      tradeFilter === m
                        ? m === 'live' ? 'bg-profit/20 text-profit' : m === 'demo' ? 'bg-warning/20 text-warning' : 'bg-accent/20 text-accent'
                        : 'text-muted hover:text-white'
                    }`}>
                    {m === 'all' ? 'All' : m === 'live' ? '🟢 Live' : '🟡 Demo'}
                  </button>
                ))}
              </div>
              <div className="flex gap-1 bg-elevated rounded-xl p-1">
                {(['all', 'robinhood', 'capital'] as const).map(b => (
                  <button key={b} onClick={() => setBrokerFilter(b)}
                    className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors ${
                      brokerFilter === b ? 'bg-accent/20 text-accent' : 'text-muted hover:text-white'
                    }`}>
                    {b === 'all' ? 'All Brokers' : b === 'robinhood' ? '🪙 Robinhood' : '📈 Capital.com'}
                  </button>
                ))}
              </div>
              {/* Summary pills */}
              {stats && (
                <div className="flex gap-2 ml-auto text-xs">
                  <span className="px-2 py-1 rounded-lg bg-elevated text-muted">
                    {stats.total} trades · {stats.win_rate}% win
                    {(stats as any).system_closes > 0 && <span className="ml-1 opacity-50">+{(stats as any).system_closes} restarts</span>}
                  </span>
                  <span className={`px-2 py-1 rounded-lg font-mono font-semibold ${(stats.total_pnl ?? 0) >= 0 ? 'bg-profit/10 text-profit' : 'bg-loss/10 text-loss'}`}>
                    {(stats.total_pnl ?? 0) >= 0 ? '+' : ''}${(stats.total_pnl ?? 0).toFixed(2)}
                  </span>
                </div>
              )}
            </div>

            {/* Trade list */}
            <div className="space-y-2">
              {trades.length === 0 ? (
                <div className="py-16 flex flex-col items-center gap-3 text-muted">
                  <span className="text-4xl">📭</span>
                  <p className="text-sm">No trades yet. Start a bot to begin.</p>
                </div>
              ) : trades.map(trade => (
                <div key={trade.id}>
                  <button
                    className={`w-full text-left p-4 rounded-xl border transition-colors ${
                      trade.is_system_close
                        ? 'opacity-40 border-border bg-card cursor-default'
                        : expandedTrade === trade.id ? 'border-accent/30 bg-elevated' : 'border-border bg-card hover:border-border/80 hover:bg-elevated/50'
                    }`}
                    onClick={() => !trade.is_system_close && setExpandedTrade(expandedTrade === trade.id ? null : trade.id)}
                  >
                    <div className="flex items-center gap-3">
                      {/* Side badge */}
                      <span className={`w-10 h-10 rounded-xl flex items-center justify-center text-sm font-bold flex-shrink-0 ${
                        trade.side === 'buy' ? 'bg-profit/15 text-profit' : 'bg-loss/15 text-loss'
                      }`}>
                        {trade.side === 'buy' ? '↑' : '↓'}
                      </span>

                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-semibold text-sm">{trade.symbol}</span>
                          <span className="text-xs text-muted">{trade.side.toUpperCase()}</span>
                          {trade.is_demo
                            ? <span className="text-xs px-1.5 py-0.5 rounded bg-warning/10 text-warning font-medium">DEMO</span>
                            : <span className="text-xs px-1.5 py-0.5 rounded bg-profit/10 text-profit font-medium">LIVE</span>
                          }
                          {trade.state === 'open' && <span className="text-xs px-1.5 py-0.5 rounded bg-accent/10 text-accent font-medium">OPEN</span>}
                          {trade.is_system_close && <span className="text-xs px-1.5 py-0.5 rounded bg-muted/10 text-muted font-medium">Server Restart</span>}
                          {!trade.is_system_close && trade.ai?.grade && trade.ai.grade !== 'N/A' && <GradeBadge grade={trade.ai.grade} />}
                        </div>
                        <p className="text-xs text-muted mt-0.5">
                          {trade.opened_at ? new Date(trade.opened_at).toLocaleString() : '—'}
                          {!trade.is_system_close && trade.exit_reason && ` · ${trade.exit_reason}`}
                        </p>
                      </div>

                      <div className="text-right flex-shrink-0">
                        {trade.is_system_close ? (
                          <p className="text-xs text-muted italic">closed on restart</p>
                        ) : trade.pnl !== null ? (
                          <p className={`font-mono font-bold text-sm ${trade.pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                            {trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(2)}
                          </p>
                        ) : (
                          <p className="text-muted text-sm">—</p>
                        )}
                        {!trade.is_system_close && trade.entry_price && (
                          <p className="text-xs text-muted font-mono">${parseFloat(trade.entry_price).toLocaleString('en-US', { minimumFractionDigits: 2 })}</p>
                        )}
                      </div>
                    </div>
                  </button>

                  {/* Expanded details */}
                  {expandedTrade === trade.id && (
                    <div className="mx-2 p-4 rounded-b-xl bg-elevated border border-t-0 border-border/50 space-y-3">
                      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                        {[
                          { label: 'Entry', val: trade.entry_price ? `$${parseFloat(trade.entry_price).toLocaleString()}` : '—' },
                          { label: 'Exit', val: trade.exit_price ? `$${parseFloat(trade.exit_price).toLocaleString()}` : '—' },
                          { label: 'Quantity', val: formatQty(trade.quantity) },
                          { label: 'P&L %', val: trade.pnl_pct !== null ? `${trade.pnl_pct.toFixed(2)}%` : '—' },
                        ].map((f, i) => (
                          <div key={i} className="p-2.5 rounded-lg bg-card">
                            <p className="text-muted mb-1">{f.label}</p>
                            <p className="font-mono font-semibold text-white">{f.val}</p>
                          </div>
                        ))}
                      </div>
                      {trade.ai?.analyzed && (
                        <div className="p-3 rounded-xl bg-purple-900/20 border border-purple-500/20 space-y-2">
                          <div className="flex items-center gap-2">
                            <span className="text-xs">🧠 AI Analysis</span>
                            <GradeBadge grade={trade.ai.grade} />
                          </div>
                          {trade.ai.what_went_well?.length > 0 && (
                            <div className="text-xs text-profit">✓ {trade.ai.what_went_well[0]}</div>
                          )}
                          {trade.ai.improvements?.length > 0 && (
                            <div className="text-xs text-warning">→ {trade.ai.improvements[0]}</div>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}

        {/* ════════════════════════ SETTINGS TAB ════════════════════════ */}
        {activeTab === 'settings' && (
          <div className="space-y-5">

            {/* ── Broker Setup ── */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h3 className="font-semibold mb-4">Broker Setup</h3>

              {/* Broker tabs */}
              <div className="flex gap-1.5 p-1 bg-elevated rounded-xl mb-5">
                {([
                  { val: 'robinhood', label: '🪙 Robinhood', sub: 'BTC · ETH · SOL' },
                  { val: 'capital',   label: '📈 Capital.com', sub: 'GOLD · US100' },
                  { val: 'tradovate', label: '⚡ Tradovate', sub: 'Futures' },
                ] as const).map(b => (
                  <button key={b.val} onClick={() => setBrokerKeysTab(b.val)}
                    className={`flex-1 py-2 rounded-lg text-xs font-semibold transition-colors ${brokerKeysTab === b.val ? 'bg-accent text-white' : 'text-muted hover:text-white'}`}>
                    <span className="block">{b.label}</span>
                    <span className="text-xs opacity-60 font-normal">{b.sub}</span>
                  </button>
                ))}
              </div>

              {/* Robinhood */}
              {brokerKeysTab === 'robinhood' && (
                <div className="space-y-4">
                  <div className="p-3 rounded-xl bg-accent/5 border border-accent/20 text-xs text-muted">
                    Register your public key on <span className="text-accent">robinhood.com → Investing → API Integrations</span>
                  </div>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Your Public Key</label>
                    <div className="flex gap-2">
                      <input readOnly value={settings?.public_key ?? ''} className="flex-1 px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-xs font-mono focus:outline-none" />
                      <button onClick={() => { navigator.clipboard.writeText(settings?.public_key ?? ''); showToast('Copied!') }}
                        className="px-4 py-2.5 rounded-xl bg-elevated border border-border text-xs text-muted hover:text-white transition-colors">Copy</button>
                    </div>
                  </div>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Robinhood API Key</label>
                    <input type="text" value={rhApiKey} onChange={e => setRhApiKey(e.target.value)} placeholder="rh-api-key-..."
                      className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                  </div>
                  <div className="flex gap-2">
                    <button onClick={saveKeys} disabled={keysLoading}
                      className="px-5 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 text-sm font-medium transition-colors">
                      {keysLoading ? 'Saving...' : 'Save Key'}
                    </button>
                    {settings?.has_api_keys && (
                      <button onClick={testConnection} disabled={testLoading}
                        className="px-4 py-2.5 rounded-xl bg-elevated border border-border hover:border-accent/50 disabled:opacity-50 text-sm text-muted hover:text-white transition-colors">
                        {testLoading ? 'Testing...' : 'Test'}
                      </button>
                    )}
                  </div>
                  {testResult && (
                    <p className={`text-xs px-3 py-2 rounded-lg ${testResult.ok ? 'bg-profit/10 border border-profit/30 text-profit' : 'bg-loss/10 border border-loss/30 text-loss'}`}>
                      {testResult.ok ? '✓ ' : '✗ '}{testResult.msg}
                    </p>
                  )}
                </div>
              )}

              {/* Capital.com */}
              {brokerKeysTab === 'capital' && (
                <div className="space-y-4">
                  <div className="p-3 rounded-xl bg-accent/5 border border-accent/20 text-xs text-muted">
                    Get your API key: <span className="text-accent">capital.com → Settings → API Integrations → Generate Key</span>
                  </div>
                  {[
                    { label: 'API Key', val: capitalApiKey, set: setCapitalApiKey, placeholder: 'your-capital-api-key', type: 'text' },
                    { label: 'Login Email', val: capitalIdentifier, set: setCapitalIdentifier, placeholder: 'you@example.com', type: 'email' },
                    { label: 'Login Password', val: capitalPassword, set: setCapitalPassword, placeholder: '••••••••', type: 'password' },
                  ].map(f => (
                    <div key={f.label}>
                      <label className="block text-xs text-muted mb-1.5">{f.label}</label>
                      <input type={f.type} value={f.val} onChange={e => f.set(e.target.value)} placeholder={f.placeholder}
                        className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                    </div>
                  ))}
                  <div className="flex gap-2">
                    <button onClick={saveCapitalKeys} disabled={capitalKeyLoading}
                      className="px-5 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 text-sm font-medium transition-colors">
                      {capitalKeyLoading ? 'Saving...' : 'Save Keys'}
                    </button>
                    {settings?.has_capital_keys && (
                      <button onClick={testCapitalConnection} disabled={capitalTestLoading}
                        className="px-4 py-2.5 rounded-xl bg-elevated border border-border hover:border-accent/50 disabled:opacity-50 text-sm text-muted hover:text-white transition-colors">
                        {capitalTestLoading ? 'Testing...' : 'Test Connection'}
                      </button>
                    )}
                  </div>
                  {capitalTestResult && (
                    <p className={`text-xs px-3 py-2 rounded-lg ${capitalTestResult.ok ? 'bg-profit/10 border border-profit/30 text-profit' : 'bg-loss/10 border border-loss/30 text-loss'}`}>
                      {capitalTestResult.ok ? '✓ ' : '✗ '}{capitalTestResult.msg}
                    </p>
                  )}
                </div>
              )}

              {/* Tradovate */}
              {brokerKeysTab === 'tradovate' && (
                <div className="space-y-4">
                  <div className="p-3 rounded-xl bg-accent/5 border border-accent/20 text-xs text-muted">
                    Account ID: <span className="text-accent">Tradovate platform → Account → Account Details</span>
                  </div>
                  {[
                    { label: 'Username', val: tradovateUsername, set: setTradovateUsername, placeholder: 'username', type: 'text' },
                    { label: 'Password', val: tradovatePassword, set: setTradovatePassword, placeholder: '••••••••', type: 'password' },
                    { label: 'Account ID', val: tradovateAccountId, set: setTradovateAccountId, placeholder: '12345', type: 'number' },
                  ].map(f => (
                    <div key={f.label}>
                      <label className="block text-xs text-muted mb-1.5">{f.label}</label>
                      <input type={f.type} value={f.val} onChange={e => f.set(e.target.value)} placeholder={f.placeholder}
                        className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                    </div>
                  ))}
                  <div className="flex gap-2">
                    <button onClick={saveTradovateKeys} disabled={tradovateKeyLoading}
                      className="px-5 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 text-sm font-medium transition-colors">
                      {tradovateKeyLoading ? 'Saving...' : 'Save Keys'}
                    </button>
                    {settings?.has_tradovate_keys && (
                      <button onClick={testTradovateConnection} disabled={tradovateTestLoading}
                        className="px-4 py-2.5 rounded-xl bg-elevated border border-border hover:border-accent/50 disabled:opacity-50 text-sm text-muted hover:text-white transition-colors">
                        {tradovateTestLoading ? 'Testing...' : 'Test Connection'}
                      </button>
                    )}
                  </div>
                  {tradovateTestResult && (
                    <p className={`text-xs px-3 py-2 rounded-lg ${tradovateTestResult.ok ? 'bg-profit/10 border border-profit/30 text-profit' : 'bg-loss/10 border border-loss/30 text-loss'}`}>
                      {tradovateTestResult.ok ? '✓ ' : '✗ '}{tradovateTestResult.msg}
                    </p>
                  )}
                </div>
              )}
            </div>

            {/* ── Trading Settings ── */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h3 className="font-semibold mb-4">Trading Settings</h3>
              <div className="space-y-4">
                {/* Broker */}
                <div>
                  <label className="block text-xs text-muted mb-1.5">Broker</label>
                  <div className="flex gap-1.5 p-1 bg-elevated rounded-xl">
                    {([
                      { val: 'robinhood', label: '🪙 Robinhood' },
                      { val: 'capital',   label: '📈 Capital.com' },
                      { val: 'tradovate', label: '⚡ Tradovate' },
                    ] as const).map(b => (
                      <button key={b.val} onClick={() => {
                        setFormBroker(b.val)
                        setFormSymbol(b.val === 'robinhood' ? 'BTC-USD' : b.val === 'capital' ? 'GOLD' : 'GC')
                      }}
                        className={`flex-1 py-2 text-xs font-semibold rounded-lg transition-colors ${formBroker === b.val ? 'bg-accent text-white' : 'text-muted hover:text-white'}`}>
                        {b.label}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Symbol */}
                <div>
                  <label className="block text-xs text-muted mb-1.5">Instrument</label>
                  <select value={formSymbol} onChange={e => setFormSymbol(e.target.value)}
                    className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent">
                    {formBroker === 'robinhood' && ['BTC-USD', 'ETH-USD', 'SOL-USD', 'DOGE-USD'].map(s => <option key={s}>{s}</option>)}
                    {formBroker === 'capital' && [{ v: 'GOLD', l: 'Gold (XAU/USD)' }, { v: 'US100', l: 'NASDAQ 100' }].map(s => <option key={s.v} value={s.v}>{s.l}</option>)}
                    {formBroker === 'tradovate' && [{ v: 'GC', l: 'Gold Futures (GC)' }, { v: 'NQ', l: 'NAS100 Futures (NQ)' }].map(s => <option key={s.v} value={s.v}>{s.l}</option>)}
                  </select>
                </div>

                {/* Quick presets */}
                <div>
                  <label className="block text-xs text-muted mb-1.5">Quick Preset</label>
                  <div className="flex flex-wrap gap-2">
                    {formBroker === 'robinhood' && (
                      <button onClick={() => { setFormStopLoss(0.005); setFormTakeProfit(0.02); setFormTrailStop(0.005) }}
                        className="px-3 py-1.5 text-xs rounded-lg bg-elevated border border-border hover:border-accent/50 text-muted hover:text-white transition-colors">
                        Crypto (SL 0.5% / TP 2%)
                      </button>
                    )}
                    {formBroker !== 'robinhood' && [
                      { label: 'Gold (SL 0.8% / TP 1.6%)', sl: 0.008, tp: 0.016, trail: 0.005 },
                      { label: 'NAS100 (SL 0.5% / TP 1.2%)', sl: 0.005, tp: 0.012, trail: 0.004 },
                    ].map(p => (
                      <button key={p.label} onClick={() => { setFormStopLoss(p.sl); setFormTakeProfit(p.tp); setFormTrailStop(p.trail) }}
                        className="px-3 py-1.5 text-xs rounded-lg bg-elevated border border-border hover:border-accent/50 text-muted hover:text-white transition-colors">
                        {p.label}
                      </button>
                    ))}
                  </div>
                </div>

                {/* SL / TP / Trail */}
                <div className="grid grid-cols-3 gap-3">
                  {[
                    { label: 'Stop Loss %', val: formStopLoss, set: setFormStopLoss, step: 0.005 },
                    { label: 'Take Profit %', val: formTakeProfit, set: setFormTakeProfit, step: 0.005 },
                    { label: 'Trail Stop %', val: formTrailStop, set: setFormTrailStop, step: 0.005 },
                  ].map(f => (
                    <div key={f.label}>
                      <label className="block text-xs text-muted mb-1.5">{f.label}</label>
                      <input type="number" step={f.step} min={0.005} value={f.val}
                        onChange={e => f.set(parseFloat(e.target.value))}
                        className="w-full px-3 py-2 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent" />
                      <p className="text-xs text-muted/60 mt-1">{(f.val * 100).toFixed(1)}%</p>
                    </div>
                  ))}
                </div>

                <button onClick={requestSaveSettings} disabled={settingsLoading}
                  className="w-full py-3 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 font-semibold text-sm transition-colors">
                  {settingsLoading ? 'Saving...' : 'Save Settings'}
                </button>
              </div>
            </div>

            {/* ── Notifications ── */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h3 className="font-semibold mb-4">📱 Telegram Notifications</h3>
              <div className="space-y-3">
                <div>
                  <label className="block text-xs text-muted mb-1.5">Bot Token</label>
                  <input type="text" value={telegramToken} onChange={e => setTelegramToken(e.target.value)} placeholder="1234567890:ABC..."
                    className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                </div>
                <div>
                  <label className="block text-xs text-muted mb-1.5">Chat ID</label>
                  <input type="text" value={telegramChatId} onChange={e => setTelegramChatId(e.target.value)} placeholder="-100..."
                    className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                </div>
                <div className="flex gap-2">
                  <button onClick={saveTelegram} disabled={telegramLoading}
                    className="px-5 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 text-sm font-medium transition-colors">
                    {telegramLoading ? 'Saving...' : 'Save'}
                  </button>
                  {settings?.telegram_configured && (
                    <button onClick={testTelegram} disabled={telegramLoading}
                      className="px-4 py-2.5 rounded-xl bg-elevated border border-border hover:border-accent/50 text-sm text-muted hover:text-white transition-colors">
                      Send Test
                    </button>
                  )}
                </div>
              </div>
            </div>

            {/* ── Account ── */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h3 className="font-semibold mb-4">Account</h3>
              <div className="flex items-center justify-between py-3 border-b border-border/50">
                <div>
                  <p className="text-sm font-medium">{user?.email}</p>
                  <p className="text-xs text-muted mt-0.5">{isPremium ? 'Nalo.Ai Pro' : 'Free Plan'}</p>
                </div>
                {isPremium
                  ? <button onClick={manageSubscription} className="text-xs px-3 py-1.5 rounded-lg border border-border text-muted hover:text-white transition-colors">Manage Plan</button>
                  : <button onClick={() => setShowPremiumModal(true)} className="text-xs px-3 py-1.5 rounded-lg bg-purple-600 hover:bg-purple-500 text-white font-semibold transition-colors">Upgrade</button>
                }
              </div>
              <button onClick={handleLogout} className="mt-4 text-xs text-muted hover:text-loss transition-colors">Sign out</button>
            </div>
          </div>
        )}
      </main>

      {/* ── Confirm Modal ── */}
      {showConfirm && pendingSettings && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 px-4">
          <div className="bg-card border border-border rounded-2xl p-6 max-w-sm w-full shadow-2xl">
            <h3 className="font-bold text-base mb-3">Save Settings?</h3>
            <div className="space-y-2 text-sm text-muted mb-5">
              <div className="flex justify-between"><span>Broker</span><span className="text-white font-medium">{pendingSettings.broker_type}</span></div>
              <div className="flex justify-between"><span>Symbol</span><span className="text-white font-medium">{pendingSettings.trading_symbol}</span></div>
              <div className="flex justify-between"><span>Stop Loss</span><span className="text-white font-medium">{(pendingSettings.stop_loss_pct * 100).toFixed(1)}%</span></div>
              <div className="flex justify-between"><span>Take Profit</span><span className="text-white font-medium">{(pendingSettings.take_profit_pct * 100).toFixed(1)}%</span></div>
            </div>
            <div className="flex gap-3">
              <button onClick={() => setShowConfirm(false)} className="flex-1 py-2.5 rounded-xl border border-border text-sm text-muted hover:text-white transition-colors">Cancel</button>
              <button onClick={confirmSaveSettings} className="flex-1 py-2.5 rounded-xl bg-accent hover:bg-blue-500 text-sm font-semibold transition-colors">Confirm</button>
            </div>
          </div>
        </div>
      )}

      {/* ── Premium Modal ── */}
      {showPremiumModal && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 px-4">
          <div className="bg-card border border-purple-500/30 rounded-2xl p-6 max-w-md w-full shadow-2xl">
            <div className="flex items-center gap-3 mb-4">
              <span className="text-3xl">🧠</span>
              <div>
                <h3 className="font-bold text-lg">Nalo.Ai Pro</h3>
                <p className="text-sm text-muted">AI-powered auto-calibration</p>
              </div>
            </div>
            <div className="space-y-2 mb-5">
              {[
                'AI analyses every trade after it closes',
                'Strategy parameters auto-calibrate for better returns',
                'Your bot improves with every trade, automatically',
                'Priority support',
              ].map((f, i) => (
                <div key={i} className="flex items-center gap-2 text-sm">
                  <span className="text-profit">✓</span>
                  <span>{f}</span>
                </div>
              ))}
            </div>
            <div className="flex gap-3">
              <button onClick={() => setShowPremiumModal(false)} className="flex-1 py-2.5 rounded-xl border border-border text-sm text-muted hover:text-white transition-colors">Cancel</button>
              <button onClick={activatePremium} disabled={premiumLoading}
                className="flex-1 py-2.5 rounded-xl bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-sm font-semibold transition-colors shadow-lg shadow-purple-600/20">
                {premiumLoading ? 'Loading...' : 'Upgrade — $199/mo'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
