import React, { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { AreaChart, Area, LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, ScatterChart, Scatter, ComposedChart, Bar } from 'recharts'
import { useAuth } from '../contexts/AuthContext'
import { api, getAccessToken } from '../api/axios'

// ── Types ─────────────────────────────────────────────────────────────────────

interface BotStatus {
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
  const [botStatus, setBotStatus] = useState<BotStatus | null>(null)
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
  const [feed, setFeed] = useState<LiveEvent[]>([])
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null)
  const [botLoading, setBotLoading] = useState(false)
  // Daily compounding target
  const [dailyPnl, setDailyPnl] = useState(0)
  const [dailyTarget, setDailyTarget] = useState(200)
  const [dailyProgressPct, setDailyProgressPct] = useState(0)
  const [settingsLoading, setSettingsLoading] = useState(false)
  const [keysLoading, setKeysLoading] = useState(false)
  const [balance, setBalance] = useState<Balance | null>(null)
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
    const results = await Promise.allSettled([
      api.get('/api/bot/status'),
      api.get(`/api/trades?limit=20${modeParam}`),
      api.get(`/api/trades/stats?mode=${tradeFilter}`),
      api.get('/api/reports/latest'),
      api.get('/api/bot/settings'),
      api.get('/api/trades/stats?mode=demo'),
      api.get('/api/trades/stats?mode=live'),
    ])
    const [statusR, tradesR, statsR, reportR, settingsR, demoStatsR, liveStatsR] = results
    if (statusR.status === 'fulfilled') setBotStatus(statusR.value.data)
    if (tradesR.status === 'fulfilled') setTrades(tradesR.value.data)
    if (statsR.status === 'fulfilled') setStats(statsR.value.data)
    if (demoStatsR.status === 'fulfilled') setDemoStats(demoStatsR.value.data)
    if (liveStatsR.status === 'fulfilled') setLiveStats(liveStatsR.value.data)
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
    // Fetch premium status
    api.get('/api/bot/premium/status').then(r => {
      setIsPremium(r.data.is_premium)
      setPremiumData(r.data)
      if (r.data.recent_calibrations) setCalibrations(r.data.recent_calibrations)
    }).catch(() => {})
  }, [tradeFilter])

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
          if (d.demo_balance != null) setLiveDemoBalance(d.demo_balance)
          if (typeof d.daily_pnl === 'number') setDailyPnl(d.daily_pnl)
          if (typeof d.daily_target === 'number') setDailyTarget(d.daily_target)
          if (typeof d.daily_progress_pct === 'number') setDailyProgressPct(d.daily_progress_pct)
          if (!isPrimary) return
          if (d.price) setLivePrice(d.price)
          if (typeof d.z_score === 'number') setLiveZ(d.z_score)
          if (d.price) {
            setPriceHistory(prev => [...prev.slice(-120), { time: new Date().toLocaleTimeString(), price: d.price, z: d.z_score }])
          }
          setBotStatus(prev => prev
            ? { ...prev, running: true, in_trade: d.in_trade, entry_price: d.entry_price, trade_side: d.trade_side, trail_stop: d.trail_stop, last_signal: d.last_signal, demo_mode: d.demo_mode, indicators: d.indicators, position_size: d.position_size, risk: d.risk, ...(d.key_invalid != null ? { key_invalid: d.key_invalid } : {}) }
            : null)
        } else if (d.type === 'trade_opened') {
          if (d.demo_balance != null) setLiveDemoBalance(d.demo_balance)
          addFeed('trade_opened', `${String(d.side || 'UNKNOWN').toUpperCase()} ${d.symbol} @ $${typeof d.entry_price === 'number' ? d.entry_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : 'N/A'}${d.demo_mode ? ' [DEMO]' : ''}`, 'text-profit')
          loadData()
        } else if (d.type === 'trade_closed') {
          const pnl = (typeof d.pnl === 'number' && !isNaN(d.pnl)) ? d.pnl : 0
          if (d.demo_balance != null) setLiveDemoBalance(d.demo_balance)
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
          addFeed('ai', `Pro: Strategy calibrated \u2014 ${d.summary}`, 'text-purple-400')
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
      if (botStatus?.running) return
      try {
        const res = await api.get(`/api/market/price?symbol=${symbol}`)
        if (res.data.price && !stale) setLivePrice(res.data.price)
      } catch {}
    }
    fetchPrice()
    const interval = setInterval(fetchPrice, 5000)
    return () => { stale = true; clearInterval(interval) }
  }, [settings?.trading_symbol, botStatus?.running])

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

  const startBot = async (mode: 'demo' | 'live') => {
    setBotLoading(true)
    try {
      const res = await api.post('/api/bot/start', { mode })
      await refreshUser()
      const r = await api.get('/api/bot/status')
      setBotStatus(r.data)
      showToast(mode === 'live' ? 'Bot started in LIVE mode' : 'Bot started in Demo mode')
      setTradeFilter(mode === 'live' ? 'live' : 'demo')
      if (res.data?.mode) addFeed('connected', `Bot running in ${res.data.mode.toUpperCase()} mode`, mode === 'live' ? 'text-profit' : 'text-warning')
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      showToast(detail ?? 'Failed to start bot', false)
    } finally { setBotLoading(false) }
  }

  const stopBot = async () => {
    setBotLoading(true)
    try {
      await api.post('/api/bot/stop')
      await refreshUser()
      const r = await api.get('/api/bot/status')
      setBotStatus(r.data)
      showToast('Bot stopped')
    } catch { showToast('Failed to stop bot', false) }
    finally { setBotLoading(false) }
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

  const currentPnl = botStatus?.in_trade && botStatus.entry_price && botStatus.entry_price > 0 && livePrice
    ? botStatus.trade_side === 'buy'
      ? ((livePrice - botStatus.entry_price) / botStatus.entry_price) * 100
      : ((botStatus.entry_price - livePrice) / botStatus.entry_price) * 100
    : null

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-dark text-white">
      {toast && <Toast msg={toast.msg} ok={toast.ok} />}

      {/* Top Bar */}
      <header className="sticky top-0 z-40 border-b border-border bg-dark/95 backdrop-blur-md">
        <div className="max-w-7xl mx-auto px-4 h-14 flex items-center justify-between gap-4">
          <div className="flex items-center gap-2 flex-shrink-0">
            <span className="text-xl">&#9889;</span>
            <span className="font-bold text-sm" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>Nalo.Ai</span>
            {isPremium && <span className="text-xs px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-400 border border-purple-500/30 font-medium">PRO</span>}
            {isDemo
              ? <span className="text-xs px-2 py-0.5 rounded-full bg-warning/20 text-warning border border-warning/30 font-medium">DEMO</span>
              : botStatus?.running && !botStatus.demo_mode
                ? <span className="text-xs px-2 py-0.5 rounded-full bg-profit/20 text-profit border border-profit/30 font-medium">LIVE</span>
                : null}
          </div>
          <div className="flex items-center gap-3 text-sm font-mono">
            <span className="text-muted text-xs hidden sm:block">{settings?.trading_symbol ?? 'BTC-USD'}</span>
            {livePrice ? <span className="text-white font-semibold">${livePrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span> : <span className="text-muted text-xs animate-pulse">fetching...</span>}
            {/* Z-score hidden from users */}
          </div>
          <div className="flex items-center gap-2">
            {user?.is_admin && (
              <button onClick={() => navigate('/admin')} className="text-xs px-3 py-1.5 rounded-lg bg-purple-500/15 border border-purple-500/30 text-purple-400 hover:bg-purple-500/25 transition-colors font-semibold">Admin</button>
            )}
            <span className="text-xs text-muted hidden md:block">{user?.email}</span>
            <button onClick={handleLogout} className="text-xs px-3 py-1.5 rounded-lg border border-border text-muted hover:text-white hover:border-accent/50 transition-colors">Logout</button>
          </div>
        </div>
      </header>

      {/* Tab Navigation */}
      <div className="max-w-7xl mx-auto px-4 pt-4">
        <div className="flex gap-2 mb-5">
          {(['dashboard', 'trades', 'analytics', 'settings'] as const).map(tab => (
            <TabButton key={tab} label={tab.charAt(0).toUpperCase() + tab.slice(1)} active={activeTab === tab} onClick={() => setActiveTab(tab)} />
          ))}
        </div>
      </div>

      <main className="max-w-7xl mx-auto px-4 pb-6 space-y-5">

        {/* ═══════════════ DASHBOARD TAB ═══════════════ */}
        {activeTab === 'dashboard' && (
          <>
            {/* Mode Banner */}
            {isDemo ? (
              <div className="px-5 py-3 rounded-xl bg-warning/10 border border-warning/30 flex items-start gap-3">
                <span className="text-xl mt-0.5">&#127917;</span>
                <div>
                  <p className="text-sm font-semibold text-warning">Demo Mode Active</p>
                  <p className="text-xs text-muted mt-0.5">Simulated trades with virtual balance. No real money at risk. Add your Robinhood API keys in Settings to trade for real.</p>
                </div>
              </div>
            ) : botStatus?.running && !botStatus.demo_mode ? (
              <div className="px-5 py-3 rounded-xl bg-profit/10 border border-profit/30 flex items-start gap-3">
                <span className="text-xl mt-0.5">&#128176;</span>
                <div>
                  <p className="text-sm font-semibold text-profit">Live Trading Active</p>
                  <p className="text-xs text-muted mt-0.5">Real orders are being placed on Robinhood. Your capital is at risk.</p>
                </div>
              </div>
            ) : null}

            {/* Key Invalid Warning */}
            {botStatus?.key_invalid && (
              <div className="flex items-start gap-3 p-4 rounded-2xl bg-loss/10 border border-loss/30">
                <span className="text-loss text-lg flex-shrink-0">&#9888;</span>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-semibold text-loss">Robinhood API Key Invalid</p>
                  <p className="text-xs text-muted mt-0.5">Open Settings and update your API key.</p>
                </div>
                <button onClick={() => setActiveTab('settings')} className="flex-shrink-0 text-xs px-3 py-1.5 rounded-lg bg-loss/20 border border-loss/30 text-loss hover:bg-loss/30 transition-colors font-medium">Settings &rarr;</button>
              </div>
            )}

            {/* Risk Pause Warning */}
            {botStatus?.risk?.is_paused && (
              <div className="flex items-start gap-3 p-4 rounded-2xl bg-warning/10 border border-warning/30">
                <span className="text-warning text-lg flex-shrink-0">&#9888;&#65039;</span>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-semibold text-warning">Trading Paused by Risk Manager</p>
                  <p className="text-xs text-muted mt-0.5">{botStatus.risk.pause_reason}</p>
                </div>
                <button onClick={resumeTrading} className="flex-shrink-0 text-xs px-3 py-1.5 rounded-lg bg-accent/20 border border-accent/30 text-accent hover:bg-accent/30 transition-colors font-medium">Resume Trading</button>
              </div>
            )}

            {/* Bot Control */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <div className="flex flex-col lg:flex-row lg:items-center gap-5">
                <div className="flex items-center gap-4 flex-shrink-0">
                  <div className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${botStatus?.running ? 'pulse-dot bg-profit' : 'bg-muted'}`} />
                  <div>
                    <span className="font-semibold text-sm">
                      {botStatus?.running ? (botStatus.demo_mode ? 'Running \u2014 Demo' : 'Running \u2014 LIVE') : 'Bot is stopped'}
                    </span>
                    {botStatus?.running && botStatus.demo_mode && liveDemoBalance != null && (
                      <span className="text-xs text-warning ml-2 font-mono">Virtual: ${liveDemoBalance.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                    )}
                    {botStatus?.last_signal && <p className="text-xs text-muted mt-0.5 truncate max-w-xs">{sanitizeSignal(botStatus.last_signal)}</p>}
                  </div>
                  <div className="flex items-center gap-2 ml-2">
                    {botStatus?.running ? (
                      <button onClick={stopBot} disabled={botLoading} className="px-4 py-1.5 rounded-lg bg-loss/20 border border-loss/40 text-loss text-xs font-semibold hover:bg-loss/30 transition-colors disabled:opacity-50">{botLoading ? '...' : 'Stop'}</button>
                    ) : (
                      <>
                        <button onClick={() => startBot('demo')} disabled={botLoading} className="px-4 py-1.5 rounded-lg bg-warning/15 border border-warning/40 text-warning text-xs font-semibold hover:bg-warning/25 transition-colors disabled:opacity-50">{botLoading ? '...' : 'Start Demo'}</button>
                        <button onClick={() => startBot('live')} disabled={botLoading || !settings?.has_api_keys || botStatus?.key_invalid} title={!settings?.has_api_keys ? 'Add API key first' : botStatus?.key_invalid ? 'API key invalid' : 'Start live'} className="px-4 py-1.5 rounded-lg bg-profit/15 border border-profit/40 text-profit text-xs font-semibold hover:bg-profit/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">{botLoading ? '...' : 'Start Live'}</button>
                      </>
                    )}
                  </div>
                </div>

                {/* Active trade details */}
                {botStatus?.in_trade && botStatus.entry_price && (
                  <div className="flex-1 p-4 rounded-xl bg-elevated border border-border">
                    <div className="flex flex-wrap gap-4 items-center">
                      {[
                        { label: 'Side', badge: true, val: botStatus.trade_side },
                        { label: 'Entry', value: `$${(botStatus.entry_price ?? 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` },
                        ...(livePrice ? [{ label: 'Current', value: `$${livePrice.toLocaleString('en-US', { minimumFractionDigits: 2 })}` }] : []),
                        ...(currentPnl !== null ? [{ label: 'Live P&L', pnl: true, val: currentPnl }] : []),
                        ...(botStatus.trail_stop ? [{ label: 'Trail Stop', value: `$${botStatus.trail_stop.toFixed(2)}`, dimmed: true }] : []),
                        { label: 'Qty', value: `${botStatus.position_size ?? '?'}` },
                      ].map((item, i) => (
                        <div key={i}>
                          <p className="text-xs text-muted">{item.label}</p>
                          {'badge' in item && item.badge ? (
                            <span className={`text-xs font-bold px-2 py-0.5 rounded ${item.val === 'buy' ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'}`}>{(item.val as string).toUpperCase()}</span>
                          ) : 'pnl' in item && item.pnl ? (
                            <p className={`font-mono font-bold text-sm ${(item.val as number) >= 0 ? 'text-profit' : 'text-loss'}`}>{(item.val as number) >= 0 ? '+' : ''}{(item.val as number).toFixed(2)}%</p>
                          ) : (
                            <p className={`font-mono text-sm ${'dimmed' in item && item.dimmed ? 'text-warning' : 'text-white'}`}>{item.value}</p>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Indicators hidden — strategy internals */}

            {/* Price Chart + Stats */}
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
              {/* Price Chart */}
              <div className="lg:col-span-2 bg-card border border-border rounded-2xl p-5">
                <h2 className="text-xs font-semibold text-muted mb-4 uppercase tracking-wider">Price Chart</h2>
                {priceHistory.length > 5 ? (
                  <ResponsiveContainer width="100%" height={220}>
                    <ComposedChart data={priceHistory}>
                      <defs>
                        <linearGradient id="priceGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#6366F1" stopOpacity={0.3} />
                          <stop offset="95%" stopColor="#6366F1" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <XAxis dataKey="time" tick={{ fontSize: 9, fill: '#A0A3B1' }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
                      <YAxis tick={{ fontSize: 10, fill: '#A0A3B1' }} axisLine={false} tickLine={false} width={72} domain={['auto', 'auto']} tickFormatter={(v: number) => `$${v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(0)}`} />
                      <Tooltip contentStyle={{ backgroundColor: '#1A1B23', border: '1px solid #2A2B35', borderRadius: 8, fontSize: 12 }} />
                      <Area type="monotone" dataKey="price" stroke="#6366F1" fill="url(#priceGrad)" strokeWidth={2} dot={false} />
                      {botStatus?.entry_price && <ReferenceLine y={botStatus.entry_price} stroke="#10B981" strokeDasharray="3 3" label={{ value: 'Entry', fill: '#10B981', fontSize: 10 }} />}
                      {botStatus?.trail_stop && <ReferenceLine y={botStatus.trail_stop} stroke="#EF4444" strokeDasharray="3 3" label={{ value: 'Trail', fill: '#EF4444', fontSize: 10 }} />}
                    </ComposedChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="h-52 flex items-center justify-center text-muted text-sm">Start the bot to see price data here.</div>
                )}
              </div>

              {/* Stats */}
              <div className="bg-card border border-border rounded-2xl p-5">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-xs font-semibold text-muted uppercase tracking-wider">Performance</h2>
                  <div className="flex gap-1 bg-elevated rounded-lg p-0.5">
                    {(['all', 'demo', 'live'] as const).map(m => (
                      <button key={m} onClick={() => setPerfMode(m)} className={`px-2.5 py-1 rounded-md text-xs font-semibold transition-colors ${perfMode === m ? m === 'live' ? 'bg-profit/20 text-profit' : m === 'demo' ? 'bg-warning/20 text-warning' : 'bg-accent/20 text-accent' : 'text-muted hover:text-white'}`}>{m === 'all' ? 'All' : m === 'live' ? 'Live' : 'Demo'}</button>
                    ))}
                  </div>
                </div>
                {(() => {
                  const s = perfMode === 'demo' ? demoStats : perfMode === 'live' ? liveStats : stats
                  return (
                    <div className="grid grid-cols-2 gap-3">
                      {[
                        { label: 'Total Trades', val: s?.total ?? '\u2014' },
                        { label: 'Win Rate', val: s ? `${s.win_rate}%` : '\u2014', color: s && s.win_rate >= 50 ? 'text-profit' : 'text-loss' },
                        { label: 'Total P&L', val: s?.total_pnl != null ? `$${Number(s.total_pnl).toFixed(2)}` : '\u2014', color: s && (s.total_pnl ?? 0) >= 0 ? 'text-profit' : 'text-loss' },
                        { label: 'Avg P&L', val: s?.avg_pnl != null ? `$${Number(s.avg_pnl).toFixed(2)}` : '\u2014', color: s && (s.avg_pnl ?? 0) >= 0 ? 'text-profit' : 'text-loss' },
                      ].map((item, i) => (
                        <div key={i} className="p-3 rounded-xl bg-elevated">
                          <p className="text-xs text-muted mb-1">{item.label}</p>
                          <p className={`text-xl font-bold font-mono ${item.color ?? 'text-white'}`}>{item.val}</p>
                        </div>
                      ))}
                    </div>
                  )
                })()}

                {/* Daily Compounding Target Progress */}
                {botStatus?.running && (
                  <div className="mt-4 pt-4 border-t border-border">
                    <div className="flex items-center justify-between mb-1.5">
                      <p className="text-xs font-semibold text-muted uppercase tracking-wider">Daily Target</p>
                      <span className={`text-xs font-mono font-bold ${dailyPnl >= dailyTarget ? 'text-profit' : dailyPnl > 0 ? 'text-warning' : 'text-muted'}`}>
                        {dailyPnl >= 0 ? '+' : ''}${dailyPnl.toFixed(2)} / ${dailyTarget.toFixed(0)}
                      </span>
                    </div>
                    <div className="h-2 rounded-full bg-elevated overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all duration-500 ${dailyPnl >= dailyTarget ? 'bg-profit' : 'bg-accent'}`}
                        style={{ width: `${Math.min(100, Math.max(0, dailyProgressPct))}%` }}
                      />
                    </div>
                    <div className="flex justify-between mt-1">
                      <span className="text-xs text-muted">{dailyProgressPct.toFixed(0)}% of goal</span>
                      {dailyPnl >= dailyTarget
                        ? <span className="text-xs text-profit font-semibold">🎯 Target hit!</span>
                        : <span className="text-xs text-muted">${Math.max(0, dailyTarget - dailyPnl).toFixed(0)} to go</span>
                      }
                    </div>
                    <p className="text-xs text-muted/60 mt-1">Target compounds with balance (2%/day)</p>
                  </div>
                )}

                {/* Balance */}
                <div className="mt-4 pt-4 border-t border-border">
                  <p className="text-xs font-semibold text-muted mb-2 uppercase tracking-wider">{isDemo ? 'Demo Balance' : 'Live Balance'}</p>
                  {isDemo ? (
                    (() => {
                      const cash = liveDemoBalance ?? settings?.demo_balance ?? 10000
                      const openPositionCost = trades
                        .filter(t => t.state === 'open' && t.is_demo)
                        .reduce((sum, t) => sum + (parseFloat(t.entry_price ?? '0') * parseFloat(t.quantity)), 0)
                      const totalPortfolio = cash + openPositionCost
                      return (
                        <div className="space-y-2 mb-2">
                          <div className="p-3 rounded-xl bg-elevated">
                            <p className="text-xs text-muted mb-1">Total Portfolio Value</p>
                            <p className="text-xl font-bold font-mono text-profit">${totalPortfolio.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</p>
                          </div>
                          <div className="flex gap-2">
                            <div className="flex-1 p-2.5 rounded-xl bg-elevated">
                              <p className="text-xs text-muted mb-0.5">Available Cash</p>
                              <p className="text-sm font-bold font-mono text-warning">${cash.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</p>
                            </div>
                            <div className="flex-1 p-2.5 rounded-xl bg-elevated">
                              <p className="text-xs text-muted mb-0.5">In Positions</p>
                              <p className="text-sm font-bold font-mono text-accent">${openPositionCost.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</p>
                            </div>
                          </div>
                        </div>
                      )
                    })()
                  ) : balance && !balance.error ? (
                    <>
                      {balance.available !== null && (
                        <div className="p-3 rounded-xl bg-elevated mb-2">
                          <p className="text-xs text-muted mb-1">Buying Power</p>
                          <p className="text-xl font-bold font-mono text-profit">${balance.available.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</p>
                        </div>
                      )}
                      {balance.holdings.length > 0 && (
                        <div className="space-y-1.5">
                          {balance.holdings.map((h, i) => (
                            <div key={i} className="flex justify-between items-center px-3 py-2 rounded-xl bg-elevated text-xs">
                              <span className="font-semibold">{h.asset_code}</span>
                              <span className="font-mono text-muted">{formatQty(h.total_quantity)}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </>
                  ) : balance?.error ? (
                    <div className="px-3 py-2 rounded-xl bg-loss/10 border border-loss/30">
                      <p className="text-xs text-loss font-medium">Balance unavailable</p>
                      <p className="text-xs text-muted mt-0.5">{balance.error}</p>
                    </div>
                  ) : null}
                </div>

                {/* Risk Status */}
                {botStatus?.risk && (
                  <div className="mt-4 pt-4 border-t border-border">
                    <p className="text-xs font-semibold text-muted mb-2 uppercase tracking-wider">Risk Manager</p>
                    <div className="space-y-1.5 text-xs">
                      <div className="flex justify-between"><span className="text-muted">Daily P&L</span><span className={botStatus.risk.daily_pnl >= 0 ? 'text-profit' : 'text-loss'}>${botStatus.risk.daily_pnl.toFixed(2)}</span></div>
                      <div className="flex justify-between"><span className="text-muted">Drawdown</span><span className={botStatus.risk.daily_drawdown_pct > 3 ? 'text-warning' : 'text-muted'}>{botStatus.risk.daily_drawdown_pct.toFixed(1)}% / {botStatus.risk.max_drawdown_pct}%</span></div>
                      <div className="flex justify-between"><span className="text-muted">Recent Stops</span><span>{botStatus.risk.recent_stops} / {botStatus.risk.max_stops}</span></div>
                      {botStatus.risk.cooldown_remaining > 0 && <div className="flex justify-between"><span className="text-muted">Cooldown</span><span className="text-warning">{botStatus.risk.cooldown_remaining} ticks</span></div>}
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Equity Curve + Live Feed */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
              <div className="bg-card border border-border rounded-2xl p-5">
                <div className="flex items-center gap-2 mb-4">
                  <h2 className="text-xs font-semibold text-muted uppercase tracking-wider">Equity Curve</h2>
                  {perfMode !== 'all' && <span className={`text-xs px-1.5 py-0.5 rounded font-semibold ${perfMode === 'live' ? 'bg-profit/15 text-profit' : 'bg-warning/15 text-warning'}`}>{perfMode.toUpperCase()}</span>}
                </div>
                {equityCurve.length > 1 ? (
                  <ResponsiveContainer width="100%" height={155}>
                    <AreaChart data={equityCurve}>
                      <defs>
                        <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#10B981" stopOpacity={0.3} />
                          <stop offset="95%" stopColor="#10B981" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <XAxis dataKey="time" tick={{ fontSize: 10, fill: '#A0A3B1' }} axisLine={false} tickLine={false} />
                      <YAxis tick={{ fontSize: 10, fill: '#A0A3B1' }} axisLine={false} tickLine={false} width={72} domain={['auto', 'auto']} tickFormatter={(v: number) => `$${v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(0)}`} />
                      <Tooltip contentStyle={{ backgroundColor: '#1A1B23', border: '1px solid #2A2B35', borderRadius: 8, fontSize: 12 }} formatter={(v: number) => [`$${v.toFixed(2)}`, 'Balance']} />
                      <Area type="monotone" dataKey="balance" stroke="#10B981" fill="url(#eqGrad)" strokeWidth={2} dot={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="h-36 flex items-center justify-center text-muted text-sm">Trade history will build the equity curve.</div>
                )}
              </div>

              <div className="bg-card border border-border rounded-2xl p-5">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-xs font-semibold text-muted uppercase tracking-wider">Live Feed</h2>
                  <div className="flex items-center gap-1.5"><div className="pulse-dot w-1.5 h-1.5 rounded-full bg-profit" /><span className="text-xs text-muted">Live</span></div>
                </div>
                <div className="space-y-2 max-h-80 overflow-y-auto">
                  {feed.length === 0 ? (
                    <p className="text-muted text-sm text-center py-10">Waiting for events...</p>
                  ) : feed.map(ev => (
                    <div key={ev.id} className="slide-in flex items-start gap-3 p-3 rounded-xl bg-elevated border border-border/50">
                      <span className={`text-sm flex-shrink-0 ${ev.color}`}>
                        {ev.type === 'trade_opened' ? '\ud83d\udcc8' : ev.type === 'trade_closed' ? '\ud83d\udcc9' : ev.type === 'ai' ? '\ud83e\udde0' : ev.type === 'error' ? '\u26a0\ufe0f' : ev.type === 'connected' ? '\ud83d\udd0c' : '\u00b7'}
                      </span>
                      <div className="flex-1 min-w-0">
                        <p className={`text-xs ${ev.color} break-words`}>{ev.message}</p>
                        <p className="text-xs text-muted/60 mt-0.5">{ev.time}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* Nalo.Ai Pro Card */}
            {!isPremium ? (
              <div className="bg-gradient-to-r from-purple-900/30 via-card to-accent/10 border border-purple-500/30 rounded-2xl p-6">
                <div className="flex flex-col md:flex-row items-start md:items-center gap-5">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-2xl">&#x1f9e0;</span>
                      <h2 className="font-bold text-lg" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>Nalo.Ai Pro</h2>
                      <span className="text-xs px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-400 border border-purple-500/30 font-semibold">$199/mo</span>
                    </div>
                    <p className="text-sm text-muted mb-3">Your bot gets smarter with every trade. AI analyzes each closed trade and auto-calibrates strategy parameters for better profitability.</p>
                    <div className="grid grid-cols-2 gap-2 text-xs">
                      {[
                        'AI trade analysis on every trade',
                        'Auto-calibration after each close',
                        'Parameters adapt to market shifts',
                        'Bot improves over time',
                      ].map((f, i) => (
                        <div key={i} className="flex items-center gap-1.5 text-purple-300">
                          <span className="text-profit">&#10003;</span> {f}
                        </div>
                      ))}
                    </div>
                  </div>
                  <button onClick={() => setShowPremiumModal(true)} className="px-6 py-3 rounded-xl bg-purple-600 hover:bg-purple-500 transition-colors font-semibold text-sm whitespace-nowrap shadow-lg shadow-purple-600/20">
                    Upgrade to Pro
                  </button>
                </div>
              </div>
            ) : (
              <div className="bg-card border border-purple-500/30 rounded-2xl p-5">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <span className="text-lg">&#x1f9e0;</span>
                    <h2 className="font-semibold text-sm" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>Nalo.Ai Pro</h2>
                    <span className="text-xs px-2 py-0.5 rounded-full bg-profit/20 text-profit border border-profit/30 font-semibold">Active</span>
                  </div>
                  <div className="flex items-center gap-3 text-xs text-muted">
                    <span>Calibrations: <strong className="text-purple-400">{premiumData?.calibration_count ?? 0}</strong></span>
                    {premiumData?.last_calibration_at && <span>Last: {new Date(premiumData.last_calibration_at).toLocaleString()}</span>}
                  </div>
                </div>
                {calibrations.length > 0 ? (
                  <div className="space-y-2">
                    {calibrations.slice(0, 3).map((cal: any, i: number) => (
                      <div key={i} className="px-4 py-3 rounded-xl bg-elevated border border-border/50">
                        <div className="flex items-start justify-between gap-3">
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 mb-1">
                              <span className="text-xs text-purple-400 font-semibold">Calibration #{premiumData?.calibration_count - i}</span>
                              <span className="text-xs text-muted">{cal.created_at ? new Date(cal.created_at).toLocaleString() : ''}</span>
                            </div>
                            {Object.keys(cal.param_changes || {}).length > 0 ? (
                              <div className="flex flex-wrap gap-1.5 mb-1">
                                {Object.entries(cal.param_changes).map(([param, change]: [string, any]) => (
                                  <span key={param} className="text-xs px-2 py-0.5 rounded bg-purple-500/15 text-purple-300 font-mono">
                                    {param}: {typeof change.old === 'number' && change.old < 1 ? `${(change.old * 100).toFixed(1)}%` : change.old} &rarr; {typeof change.new === 'number' && change.new < 1 ? `${(change.new * 100).toFixed(1)}%` : change.new}
                                  </span>
                                ))}
                              </div>
                            ) : (
                              <p className="text-xs text-muted">No parameter changes needed</p>
                            )}
                            {cal.ai_reasoning && <p className="text-xs text-muted mt-1 truncate">{cal.ai_reasoning}</p>}
                          </div>
                          <span className="text-xs text-muted flex-shrink-0">{cal.trade_count_analyzed} trades analyzed</span>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-center py-6 text-muted text-sm">
                    <p>AI auto-calibration will kick in after your next closed trade.</p>
                    <p className="text-xs mt-1 text-muted/60">Minimum 5 closed trades required for meaningful calibration.</p>
                  </div>
                )}
              </div>
            )}
          </>
        )}

        {/* ═══════════════ TRADES TAB ═══════════════ */}
        {activeTab === 'trades' && (
          <div className="bg-card border border-border rounded-2xl p-5">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xs font-semibold text-muted uppercase tracking-wider">Trade History</h2>
              <div className="flex gap-1 bg-elevated rounded-lg p-0.5">
                {(['all', 'live', 'demo'] as const).map(f => (
                  <button key={f} onClick={() => setTradeFilter(f)} className={`px-3 py-1 rounded-md text-xs font-semibold transition-colors ${tradeFilter === f ? f === 'live' ? 'bg-profit/20 text-profit' : f === 'demo' ? 'bg-warning/20 text-warning' : 'bg-accent/20 text-accent' : 'text-muted hover:text-white'}`}>{f === 'all' ? 'All' : f === 'live' ? 'Live' : 'Demo'}</button>
                ))}
              </div>
            </div>
            {trades.length === 0 ? (
              <p className="text-muted text-sm text-center py-10">No trades yet.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-muted border-b border-border">
                      {['Date', 'Symbol', 'Mode', 'Side', 'Qty', 'Entry', 'Exit', 'P&L', 'Reason', 'AI'].map((h, i) => (
                        <th key={i} className={`pb-3 font-medium ${i >= 5 ? 'text-right' : i === 9 ? 'text-center' : 'text-left'}`}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map(trade => (
                      <React.Fragment key={trade.id}>
                        <tr onClick={() => setExpandedTrade(expandedTrade === trade.id ? null : trade.id)} className="border-b border-border/40 hover:bg-elevated/50 cursor-pointer transition-colors">
                          <td className="py-3 text-muted text-xs font-mono">{trade.opened_at ? new Date(trade.opened_at).toLocaleDateString() : '\u2014'}</td>
                          <td className="py-3 font-mono font-medium">{trade.symbol}</td>
                          <td className="py-3"><span className={`text-xs px-1.5 py-0.5 rounded font-semibold ${trade.is_demo ? 'bg-warning/15 text-warning' : 'bg-profit/15 text-profit'}`}>{trade.is_demo ? 'DEMO' : 'LIVE'}</span></td>
                          <td className="py-3"><span className={`text-xs px-2 py-0.5 rounded font-bold ${trade.side === 'buy' ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'}`}>{trade.side.toUpperCase()}</span></td>
                          <td className="py-3 text-xs font-mono text-muted">{formatQty(trade.quantity)}</td>
                          <td className="py-3 text-right font-mono text-xs">{trade.entry_price ? `$${parseFloat(trade.entry_price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '\u2014'}</td>
                          <td className="py-3 text-right font-mono text-xs">{trade.exit_price ? `$${parseFloat(trade.exit_price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '\u2014'}</td>
                          <td className="py-3 text-right font-mono">
                            {trade.pnl !== null ? <span className={trade.pnl >= 0 ? 'text-profit' : 'text-loss'}>{trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(2)}</span> : <span className="text-xs text-muted">{trade.state === 'open' ? 'OPEN' : '\u2014'}</span>}
                          </td>
                          <td className="py-3 text-xs text-muted">{trade.exit_reason?.replace(/_/g, ' ') ?? '\u2014'}</td>
                          <td className="py-3 text-right"><GradeBadge grade={trade.ai?.grade} /></td>
                        </tr>
                        {expandedTrade === trade.id && trade.ai && (
                          <tr key={`${trade.id}-ai`}>
                            <td colSpan={10} className="px-4 py-4 bg-elevated/40">
                              <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
                                <div><p className="text-muted font-semibold mb-1">Entry Quality</p><p>{trade.ai.entry_quality}</p></div>
                                <div><p className="text-muted font-semibold mb-1">Exit Quality</p><p>{trade.ai.exit_quality}</p></div>
                                <div><p className="text-muted font-semibold mb-1">Confidence</p><p>{(trade.ai.confidence * 100).toFixed(0)}%</p></div>
                                {trade.ai.what_went_well.length > 0 && <div><p className="text-profit font-semibold mb-1">What went well</p><ul className="space-y-0.5">{trade.ai.what_went_well.map((w, i) => <li key={i}>&bull; {w}</li>)}</ul></div>}
                                {trade.ai.what_went_wrong.length > 0 && <div><p className="text-loss font-semibold mb-1">What went wrong</p><ul className="space-y-0.5">{trade.ai.what_went_wrong.map((w, i) => <li key={i}>&bull; {w}</li>)}</ul></div>}
                                {trade.ai.improvements.length > 0 && <div><p className="text-warning font-semibold mb-1">Improvements</p><ul className="space-y-0.5">{trade.ai.improvements.map((w, i) => <li key={i}>&bull; {w}</li>)}</ul></div>}
                              </div>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* ═══════════════ ANALYTICS TAB ═══════════════ */}
        {activeTab === 'analytics' && (
          <>
            {/* P&L Chart */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-xs font-semibold text-muted uppercase tracking-wider">P&L &mdash; Last 30 Trades</h2>
                <div className="flex gap-1 bg-elevated rounded-lg p-0.5">
                  {(['all', 'demo', 'live'] as const).map(m => (
                    <button key={m} onClick={() => setPerfMode(m)} className={`px-2.5 py-1 rounded-md text-xs font-semibold transition-colors ${perfMode === m ? m === 'live' ? 'bg-profit/20 text-profit' : m === 'demo' ? 'bg-warning/20 text-warning' : 'bg-accent/20 text-accent' : 'text-muted hover:text-white'}`}>{m === 'all' ? 'All' : m === 'live' ? 'Live' : 'Demo'}</button>
                  ))}
                </div>
              </div>
              {(() => { const s = perfMode === 'demo' ? demoStats : perfMode === 'live' ? liveStats : stats; return s?.pnl_chart?.length ? (
                <ResponsiveContainer width="100%" height={200}>
                  <AreaChart data={s.pnl_chart}>
                    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="#10B981" stopOpacity={0.3} /><stop offset="95%" stopColor="#10B981" stopOpacity={0} /></linearGradient></defs>
                    <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#A0A3B1' }} axisLine={false} tickLine={false} />
                    <YAxis tick={{ fontSize: 10, fill: '#A0A3B1' }} axisLine={false} tickLine={false} width={64} tickFormatter={(v: number) => `${v >= 0 ? '+' : ''}$${v.toFixed(0)}`} />
                    <Tooltip contentStyle={{ backgroundColor: '#1A1B23', border: '1px solid #2A2B35', borderRadius: 8, fontSize: 12 }} formatter={(v: number) => [`${v >= 0 ? '+' : ''}$${v.toFixed(2)}`, 'P&L']} />
                    <Area type="monotone" dataKey="pnl" stroke="#10B981" fill="url(#g)" strokeWidth={2} dot={false} />
                  </AreaChart>
                </ResponsiveContainer>
              ) : <div className="h-48 flex items-center justify-center text-muted text-sm">No P&L data yet for {perfMode === 'all' ? 'any' : perfMode} trades.</div>; })()}
            </div>

            {/* AI Report */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h2 className="text-xs font-semibold text-muted mb-4 uppercase tracking-wider">Latest AI Report</h2>
              {report ? (
                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-muted">{report.report_date}</span>
                    {report.full_report?.recommendation && (
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium capitalize ${report.full_report.recommendation === 'continue' ? 'bg-profit/20 text-profit' : report.full_report.recommendation === 'pause' ? 'bg-loss/20 text-loss' : 'bg-warning/20 text-warning'}`}>{report.full_report.recommendation.replace(/_/g, ' ')}</span>
                    )}
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-center">
                    {[
                      { label: 'Win Rate', val: `${report.win_rate.toFixed(1)}%`, color: report.win_rate >= 50 ? 'text-profit' : 'text-loss' },
                      { label: 'Trades', val: report.total_trades, color: 'text-white' },
                      { label: 'P&L', val: `${report.total_pnl >= 0 ? '+' : ''}$${report.total_pnl.toFixed(2)}`, color: report.total_pnl >= 0 ? 'text-profit' : 'text-loss' },
                    ].map((s, i) => (
                      <div key={i} className="p-2 rounded-lg bg-elevated"><p className="text-xs text-muted">{s.label}</p><p className={`font-bold font-mono text-sm ${s.color}`}>{s.val}</p></div>
                    ))}
                  </div>
                  {report.summary && <p className="text-sm text-muted leading-relaxed">{report.summary}</p>}
                  {report.top_improvement && (
                    <div className="px-4 py-3 rounded-xl bg-warning/10 border border-warning/20">
                      <p className="text-xs text-warning font-semibold mb-1">Top Improvement</p>
                      <p className="text-sm">{report.top_improvement}</p>
                    </div>
                  )}
                </div>
              ) : <p className="text-muted text-sm text-center py-10">Reports generated after trading activity.</p>}
            </div>
          </>
        )}

        {/* ═══════════════ SETTINGS TAB ═══════════════ */}
        {activeTab === 'settings' && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">

            {/* API Keys — 3-tab broker card */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h3 className="font-semibold mb-3 text-sm">Broker API Setup</h3>
              {/* Broker tab selector */}
              <div className="flex gap-1.5 mb-4 p-1 bg-elevated rounded-xl">
                {(['robinhood', 'capital', 'tradovate'] as const).map(b => (
                  <button key={b} onClick={() => setBrokerKeysTab(b)}
                    className={`flex-1 py-1.5 text-xs font-semibold rounded-lg transition-colors ${brokerKeysTab === b ? 'bg-accent text-white' : 'text-muted hover:text-white'}`}>
                    {b === 'robinhood' ? '🪙 Robinhood' : b === 'capital' ? '📈 Capital.com' : '⚡ Tradovate'}
                  </button>
                ))}
              </div>

              {/* ── Robinhood tab ── */}
              {brokerKeysTab === 'robinhood' && (
                <div className="space-y-3">
                  <p className="text-xs text-muted">Trade BTC, ETH, SOL, DOGE via Robinhood Crypto.</p>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Your Public Key <span className="text-muted/60">(register on Robinhood)</span></label>
                    <div className="flex gap-2">
                      <input type="text" readOnly value={settings?.public_key ?? ''} className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-xs font-mono focus:outline-none" />
                      <button onClick={() => { navigator.clipboard.writeText(settings?.public_key ?? ''); showToast('Copied!') }} className="flex-shrink-0 px-3 py-2.5 rounded-xl bg-elevated border border-border text-muted hover:text-white text-xs">Copy</button>
                    </div>
                  </div>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Robinhood API Key</label>
                    <input type="text" value={rhApiKey} onChange={e => setRhApiKey(e.target.value)} placeholder="rh-api-key-..." className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                  </div>
                  <div className="flex gap-2">
                    <button onClick={saveKeys} disabled={keysLoading} className="px-5 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 transition-colors text-sm font-medium">{keysLoading ? 'Saving...' : 'Save Key'}</button>
                    {settings?.has_api_keys && brokerKeysTab === 'robinhood' && <button onClick={testConnection} disabled={testLoading} className="px-4 py-2.5 rounded-xl bg-elevated border border-border hover:border-accent/50 disabled:opacity-50 text-sm font-medium text-muted hover:text-white">{testLoading ? 'Testing...' : 'Test'}</button>}
                  </div>
                  {testResult && <div className={`px-3 py-2 rounded-lg text-xs ${testResult.ok ? 'bg-profit/10 border border-profit/30 text-profit' : 'bg-loss/10 border border-loss/30 text-loss'}`}>{testResult.ok ? '✓ ' : '✗ '}{testResult.msg}</div>}
                </div>
              )}

              {/* ── Capital.com tab ── */}
              {brokerKeysTab === 'capital' && (
                <div className="space-y-3">
                  <p className="text-xs text-muted">Trade Gold (XAU/USD) and NAS100 as CFDs. Global access, FCA/CySEC regulated.</p>
                  <div className="px-3 py-2 rounded-xl bg-accent/5 border border-accent/20 text-xs text-muted">
                    Get your API key: <span className="text-accent">capital.com → Settings → API Integrations → Generate Key</span>
                  </div>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">API Key</label>
                    <input type="text" value={capitalApiKey} onChange={e => setCapitalApiKey(e.target.value)} placeholder="your-capital-api-key" className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm font-mono placeholder-muted/40 focus:outline-none focus:border-accent" />
                  </div>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Login Email</label>
                    <input type="email" value={capitalIdentifier} onChange={e => setCapitalIdentifier(e.target.value)} placeholder="you@example.com" className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                  </div>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Login Password</label>
                    <input type="password" value={capitalPassword} onChange={e => setCapitalPassword(e.target.value)} placeholder="••••••••" className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                  </div>
                  <div className="flex gap-2">
                    <button onClick={saveCapitalKeys} disabled={capitalKeyLoading} className="px-5 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 transition-colors text-sm font-medium">{capitalKeyLoading ? 'Saving...' : 'Save Keys'}</button>
                    {settings?.has_capital_keys && <button onClick={testCapitalConnection} disabled={capitalTestLoading} className="px-4 py-2.5 rounded-xl bg-elevated border border-border hover:border-accent/50 disabled:opacity-50 text-sm font-medium text-muted hover:text-white">{capitalTestLoading ? 'Testing...' : 'Test Connection'}</button>}
                  </div>
                  {capitalTestResult && <div className={`px-3 py-2 rounded-lg text-xs ${capitalTestResult.ok ? 'bg-profit/10 border border-profit/30 text-profit' : 'bg-loss/10 border border-loss/30 text-loss'}`}>{capitalTestResult.ok ? '✓ ' : '✗ '}{capitalTestResult.msg}</div>}
                </div>
              )}

              {/* ── Tradovate tab ── */}
              {brokerKeysTab === 'tradovate' && (
                <div className="space-y-3">
                  <p className="text-xs text-muted">Trade Gold Futures (GC) and NAS100 Futures (NQ). US-regulated CME exchange.</p>
                  <div className="px-3 py-2 rounded-xl bg-accent/5 border border-accent/20 text-xs text-muted">
                    Account ID is shown in Tradovate platform under <span className="text-accent">Account → Account Details</span>
                  </div>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Username</label>
                    <input type="text" value={tradovateUsername} onChange={e => setTradovateUsername(e.target.value)} placeholder="your_tradovate_username" className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                  </div>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Password</label>
                    <input type="password" value={tradovatePassword} onChange={e => setTradovatePassword(e.target.value)} placeholder="••••••••" className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                  </div>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Account ID</label>
                    <input type="number" value={tradovateAccountId} onChange={e => setTradovateAccountId(e.target.value)} placeholder="12345" className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                  </div>
                  <div className="flex gap-2">
                    <button onClick={saveTradovateKeys} disabled={tradovateKeyLoading} className="px-5 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 transition-colors text-sm font-medium">{tradovateKeyLoading ? 'Saving...' : 'Save Keys'}</button>
                    {settings?.has_tradovate_keys && <button onClick={testTradovateConnection} disabled={tradovateTestLoading} className="px-4 py-2.5 rounded-xl bg-elevated border border-border hover:border-accent/50 disabled:opacity-50 text-sm font-medium text-muted hover:text-white">{tradovateTestLoading ? 'Testing...' : 'Test Connection'}</button>}
                  </div>
                  {tradovateTestResult && <div className={`px-3 py-2 rounded-lg text-xs ${tradovateTestResult.ok ? 'bg-profit/10 border border-profit/30 text-profit' : 'bg-loss/10 border border-loss/30 text-loss'}`}>{tradovateTestResult.ok ? '✓ ' : '✗ '}{tradovateTestResult.msg}</div>}
                </div>
              )}
            </div>

            {/* Bot Settings */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h3 className="font-semibold mb-4 text-sm">Trading Settings</h3>
              <div className="space-y-4">
                {/* Broker selector */}
                <div>
                  <label className="block text-xs text-muted mb-1.5">Broker</label>
                  <div className="flex gap-1.5 p-1 bg-elevated rounded-xl">
                    {([
                      { val: 'robinhood', label: '🪙 Robinhood Crypto' },
                      { val: 'capital',   label: '📈 Capital.com' },
                      { val: 'tradovate', label: '⚡ Tradovate' },
                    ] as const).map(b => (
                      <button key={b.val} onClick={() => {
                        setFormBroker(b.val)
                        // Reset symbol to first option for this broker
                        const first = b.val === 'robinhood' ? 'BTC-USD' : b.val === 'capital' ? 'GOLD' : 'GC'
                        setFormSymbol(first)
                      }}
                        className={`flex-1 py-1.5 text-xs font-semibold rounded-lg transition-colors ${formBroker === b.val ? 'bg-accent text-white' : 'text-muted hover:text-white'}`}>
                        {b.label}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <label className="block text-xs text-muted mb-1.5">Trading Pair / Instrument</label>
                  <select value={formSymbol} onChange={e => setFormSymbol(e.target.value)} className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent">
                    {formBroker === 'robinhood' && ['BTC-USD', 'ETH-USD', 'SOL-USD', 'DOGE-USD'].map(s => <option key={s} value={s}>{s}</option>)}
                    {formBroker === 'capital' && [
                      { v: 'GOLD',  l: 'Gold (XAU/USD) — CFD' },
                      { v: 'US100', l: 'NASDAQ 100 (US100) — CFD' },
                    ].map(s => <option key={s.v} value={s.v}>{s.l}</option>)}
                    {formBroker === 'tradovate' && [
                      { v: 'GC', l: 'Gold Futures (GC)' },
                      { v: 'NQ', l: 'NASDAQ 100 Futures (NQ)' },
                    ].map(s => <option key={s.v} value={s.v}>{s.l}</option>)}
                  </select>
                </div>
                {/* Strategy presets by asset class */}
                <div>
                  <label className="block text-xs text-muted mb-1.5">Strategy Preset</label>
                  <div className="flex gap-2 flex-wrap">
                    {formBroker === 'robinhood' && (
                      <button onClick={() => { setFormStopLoss(0.025); setFormTakeProfit(0.05); setFormTrailStop(0.015) }}
                        className="px-3 py-1.5 text-xs rounded-lg bg-elevated border border-border hover:border-accent/50 text-muted hover:text-white transition-colors">
                        Crypto defaults (SL 2.5% / TP 5%)
                      </button>
                    )}
                    {(formBroker === 'capital' || formBroker === 'tradovate') && [
                      { label: 'Gold defaults', sl: 0.008, tp: 0.016, trail: 0.005 },
                      { label: 'NAS100 defaults', sl: 0.005, tp: 0.012, trail: 0.004 },
                    ].map(p => (
                      <button key={p.label} onClick={() => { setFormStopLoss(p.sl); setFormTakeProfit(p.tp); setFormTrailStop(p.trail) }}
                        className="px-3 py-1.5 text-xs rounded-lg bg-elevated border border-border hover:border-accent/50 text-muted hover:text-white transition-colors">
                        {p.label} (SL {(p.sl * 100).toFixed(1)}% / TP {(p.tp * 100).toFixed(1)}%)
                      </button>
                    ))}
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-3">
                  {[
                    { label: 'Stop Loss %', val: formStopLoss, set: setFormStopLoss, step: 0.005, min: 0.005, max: 0.1 },
                    { label: 'Take Profit %', val: formTakeProfit, set: setFormTakeProfit, step: 0.005, min: 0.005, max: 0.2 },
                    { label: 'Trail Stop %', val: formTrailStop, set: setFormTrailStop, step: 0.005, min: 0.005, max: 0.05 },
                  ].map((f, i) => (
                    <div key={i}><label className="block text-xs text-muted mb-1.5">{f.label}</label>
                      <input type="number" step={f.step} min={f.min} max={f.max} value={f.val} onChange={e => f.set(parseFloat(e.target.value))} className="w-full px-3 py-2 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent" />
                    </div>
                  ))}
                </div>
                <div className="px-3 py-2 rounded-xl bg-accent/5 border border-accent/20 text-xs text-muted">
                  Our AI-powered strategy uses multiple market signals to find optimal entry and exit points. All advanced filters are active by default to protect your trades.
                </div>
              </div>
            </div>

            {/* Risk Management */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h3 className="font-semibold mb-4 text-sm">Risk Management</h3>
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-3">
                  <div><label className="block text-xs text-muted mb-1.5">Max Daily Drawdown %</label><input type="number" step={0.5} min={1} max={20} value={formMaxDrawdown} onChange={e => setFormMaxDrawdown(parseFloat(e.target.value))} className="w-full px-3 py-2 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent" /></div>
                  <div><label className="block text-xs text-muted mb-1.5">Max Stop-Losses (before pause)</label><input type="number" step={1} min={1} max={10} value={formMaxStops} onChange={e => setFormMaxStops(parseInt(e.target.value))} className="w-full px-3 py-2 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent" /></div>
                  <div><label className="block text-xs text-muted mb-1.5">Cooldown Ticks (after SL)</label><input type="number" step={1} min={0} max={20} value={formCooldown} onChange={e => setFormCooldown(parseInt(e.target.value))} className="w-full px-3 py-2 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent" /></div>
                  <div><label className="block text-xs text-muted mb-1.5">Risk per Trade %</label><input type="number" step={0.25} min={0.25} max={5} value={formRiskPerTrade} onChange={e => setFormRiskPerTrade(parseFloat(e.target.value))} className="w-full px-3 py-2 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent" /></div>
                  <div className="col-span-2"><label className="block text-xs text-muted mb-1.5">Max Single Position Exposure %</label><input type="number" step={5} min={5} max={100} value={formMaxExposure} onChange={e => setFormMaxExposure(parseFloat(e.target.value))} className="w-full px-3 py-2 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent" /></div>
                </div>
              </div>
            </div>

            {/* Position Sizing */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h3 className="font-semibold mb-4 text-sm">Position Sizing</h3>
              <div className="space-y-4">
                <div className="flex gap-3">
                  {[
                    { label: 'Smart Sizing (recommended)', val: 'dynamic' },
                    { label: 'Fixed Quantity', val: 'fixed' },
                  ].map(opt => (
                    <label key={opt.val} className={`flex-1 p-3 rounded-xl border cursor-pointer text-center text-xs font-semibold transition-colors ${formPosMode === opt.val ? 'border-accent/50 bg-accent/10 text-accent' : 'border-border bg-elevated text-muted hover:text-white'}`}>
                      <input type="radio" name="posMode" value={opt.val} checked={formPosMode === opt.val} onChange={() => setFormPosMode(opt.val)} className="sr-only" />
                      {opt.label}
                    </label>
                  ))}
                </div>
                {formPosMode === 'fixed' && (
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Fixed Quantity (e.g. 0.0001 BTC)</label>
                    <input type="number" step={0.0001} min={0.0001} max={1} value={formFixedQty} onChange={e => setFormFixedQty(parseFloat(e.target.value))} className="w-full px-3 py-2 rounded-xl bg-elevated border border-border text-white text-sm font-mono focus:outline-none focus:border-accent" />
                  </div>
                )}
                {formPosMode === 'dynamic' && (
                  <div className="px-3 py-2 rounded-xl bg-elevated text-xs text-muted">
                    Automatically calculates optimal position size based on your risk settings and market volatility. Capped at {formMaxExposure}% exposure.
                  </div>
                )}
              </div>
            </div>

            {/* Demo Balance */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h3 className="font-semibold mb-4 text-sm flex items-center gap-2">Demo Balance <span className="text-xs text-warning font-normal">Paper trading</span></h3>
              <div className="flex items-end gap-3">
                <div className="flex-1">
                  <label className="block text-xs text-muted mb-1.5">Starting Balance ($)</label>
                  <input type="number" value={demoBalanceInput} onChange={e => setDemoBalanceInput(e.target.value)} min={0} step={1000} className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm font-mono focus:outline-none focus:border-accent" />
                </div>
                <button onClick={async () => {
                  setDemoBalanceLoading(true)
                  try { const res = await api.post('/api/bot/demo-balance', { balance: parseFloat(demoBalanceInput) || 10000 }); setLiveDemoBalance(res.data.balance); showToast(`Demo balance set to $${res.data.balance.toLocaleString()}`); await loadData() }
                  catch { showToast('Failed to set balance', false) }
                  finally { setDemoBalanceLoading(false) }
                }} disabled={demoBalanceLoading} className="px-4 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 text-sm font-medium">{demoBalanceLoading ? 'Setting...' : 'Set Balance'}</button>
                <button onClick={async () => {
                  if (!confirm('Reset demo balance to $10,000 and clear all demo trades?')) return
                  setDemoBalanceLoading(true)
                  try { const res = await api.post('/api/bot/demo-balance/clear'); setLiveDemoBalance(res.data.balance); setDemoBalanceInput('10000'); showToast('Demo reset'); await loadData() }
                  catch { showToast('Failed to reset', false) }
                  finally { setDemoBalanceLoading(false) }
                }} disabled={demoBalanceLoading} className="px-4 py-2.5 rounded-xl bg-loss/15 border border-loss/40 text-loss hover:bg-loss/25 disabled:opacity-50 text-sm font-medium">Clear All</button>
              </div>
            </div>

            {/* AI & Pro */}
            <div className={`bg-card border rounded-2xl p-5 ${isPremium ? 'border-purple-500/30' : 'border-border'}`}>
              <h3 className="font-semibold mb-1 text-sm flex items-center gap-2">
                AI &amp; Auto-Calibration
                {isPremium
                  ? <span className="text-xs px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-400 border border-purple-500/30 font-semibold">PRO Active</span>
                  : <span className="text-xs text-muted">Free tier</span>}
              </h3>
              {isPremium ? (
                <div className="space-y-3 mt-3">
                  <div className="px-3 py-2 rounded-xl bg-purple-500/10 border border-purple-500/20 text-xs text-purple-300">
                    AI-powered trading is active. Claude screens every signal before entry, classifies market regime in real-time, learns from your trade patterns, and auto-calibrates strategy parameters after every closed trade.
                    {premiumData?.calibration_count > 0 && <span className="block mt-1 text-muted">Total calibrations: <strong className="text-purple-400">{premiumData.calibration_count}</strong></span>}
                  </div>
                  <div className="flex gap-2">
                    <button onClick={manageSubscription} className="flex-1 py-2 rounded-xl border border-purple-500/30 text-purple-400 hover:bg-purple-500/10 text-xs font-medium transition-colors">Manage Billing</button>
                    <button onClick={deactivatePremium} disabled={premiumLoading} className="flex-1 py-2 rounded-xl border border-loss/30 text-loss/70 hover:text-loss hover:border-loss/50 text-xs transition-colors">{premiumLoading ? '...' : 'Cancel Subscription'}</button>
                  </div>
                </div>
              ) : (
                <div className="space-y-3 mt-3">
                  <p className="text-xs text-muted">AI-powered trade analysis and auto-calibration. Your bot learns from every trade and automatically optimizes for better profitability.</p>
                  <button onClick={() => setShowPremiumModal(true)} className="w-full py-2.5 rounded-xl bg-purple-600 hover:bg-purple-500 transition-colors text-sm font-semibold shadow-lg shadow-purple-600/20">Upgrade to Nalo.Ai Pro &mdash; $199/mo</button>
                </div>
              )}
            </div>

            {/* Telegram */}
            <div className="bg-card border border-border rounded-2xl p-5">
              <h3 className="font-semibold mb-1 text-sm flex items-center gap-2">Telegram Notifications {settings?.telegram_configured ? <span className="text-xs text-profit">configured</span> : <span className="text-xs text-muted">not set up</span>}</h3>
              <p className="text-xs text-muted mb-4">Get trade alerts on Telegram. Create a bot via @BotFather and get your chat ID.</p>
              <div className="space-y-3">
                <input type="text" value={telegramToken} onChange={e => setTelegramToken(e.target.value)} placeholder="Bot token from @BotFather" className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                <input type="text" value={telegramChatId} onChange={e => setTelegramChatId(e.target.value)} placeholder="Chat ID" className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent" />
                <div className="flex gap-2">
                  <button onClick={saveTelegram} disabled={telegramLoading} className="px-4 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 text-sm font-medium">{telegramLoading ? 'Saving...' : 'Save'}</button>
                  {settings?.telegram_configured && <button onClick={testTelegram} disabled={telegramLoading} className="px-4 py-2.5 rounded-xl bg-elevated border border-border hover:border-accent/50 disabled:opacity-50 text-sm font-medium text-muted hover:text-white">{telegramLoading ? '...' : 'Send Test'}</button>}
                </div>
                <label className="flex items-center gap-2 text-xs cursor-pointer">
                  <input type="checkbox" checked={formTelegramEnabled} onChange={e => setFormTelegramEnabled(e.target.checked)} className="w-4 h-4 rounded accent-accent" />
                  <span className="text-muted">Enable Telegram alerts for trades</span>
                </label>
              </div>
            </div>

            {/* Save All Settings */}
            <div className="lg:col-span-2">
              <button onClick={requestSaveSettings} disabled={settingsLoading} className="w-full py-3 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 transition-colors text-sm font-semibold">{settingsLoading ? 'Saving...' : 'Save All Settings'}</button>
            </div>
          </div>
        )}

      </main>

      {/* Premium Upgrade Modal */}
      {showPremiumModal && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={() => setShowPremiumModal(false)}>
          <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />
          <div className="relative bg-card border border-purple-500/30 rounded-2xl p-6 w-full max-w-lg shadow-2xl" onClick={e => e.stopPropagation()}>
            <div className="text-center mb-6">
              <span className="text-4xl block mb-3">&#x1f9e0;</span>
              <h2 className="text-xl font-bold" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>Upgrade to Nalo.Ai Pro</h2>
              <p className="text-muted text-sm mt-1">Your bot learns and improves with every trade</p>
            </div>

            <div className="space-y-3 mb-6">
              {[
                { icon: '&#x1f9e0;', title: 'AI Pre-Trade Screening', desc: 'Claude reviews every signal before entry — blocks bad setups, approves high-confidence trades' },
                { icon: '&#x1f4ca;', title: 'AI Trade Analysis', desc: 'Every closed trade graded with entry/exit quality, actionable feedback, and pattern detection' },
                { icon: '&#x2699;&#xfe0f;', title: 'Auto-Calibration + Quantum Optimizer', desc: 'Strategy parameters auto-tuned by both AI reasoning and quantum-inspired optimization' },
                { icon: '&#x1f9ec;', title: 'AI Pattern Memory', desc: 'Claude remembers which setups fail for you — blocks losing patterns before they repeat' },
                { icon: '&#x1f30d;', title: 'Market Regime Detection', desc: 'AI classifies trending vs ranging markets in real-time, adjusts strategy accordingly' },
                { icon: '&#x1f6e1;&#xfe0f;', title: 'Institutional Risk Management', desc: 'ATR-adaptive stops, signal-strength sizing, multi-timeframe confirmation, ETH correlation' },
              ].map((f, i) => (
                <div key={i} className="flex items-start gap-3 p-3 rounded-xl bg-elevated">
                  <span className="text-lg flex-shrink-0" dangerouslySetInnerHTML={{ __html: f.icon }} />
                  <div>
                    <p className="text-sm font-semibold">{f.title}</p>
                    <p className="text-xs text-muted">{f.desc}</p>
                  </div>
                </div>
              ))}
            </div>

            <div className="text-center mb-5">
              <span className="text-3xl font-bold" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>$199</span>
              <span className="text-muted text-sm"> /month</span>
            </div>

            <div className="flex gap-3">
              <button onClick={() => setShowPremiumModal(false)} className="flex-1 py-2.5 rounded-xl border border-border text-muted hover:text-white text-sm transition-colors">Maybe Later</button>
              <button onClick={activatePremium} disabled={premiumLoading} className="flex-1 py-2.5 rounded-xl bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-sm font-semibold transition-colors shadow-lg shadow-purple-600/20">
                {premiumLoading ? 'Activating...' : 'Activate Pro'}
              </button>
            </div>

            <p className="text-xs text-center text-muted mt-3">Cancel anytime from Settings. Stripe integration coming soon.</p>
          </div>
        </div>
      )}

      {/* Settings Confirmation Modal */}
      {showConfirm && pendingSettings && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={() => setShowConfirm(false)}>
          <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />
          <div className="relative bg-card border border-border rounded-2xl p-6 w-full max-w-md shadow-2xl max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <h3 className="font-semibold text-sm mb-4">Confirm Settings Change</h3>
            <div className="space-y-1.5 mb-5 text-xs">
              {Object.entries(pendingSettings).filter(([, v]) => typeof v !== 'boolean').map(([k, v]) => (
                <div key={k} className="flex justify-between px-3 py-1.5 rounded-lg bg-elevated">
                  <span className="text-muted">{k.replace(/_/g, ' ')}</span>
                  <span className="font-semibold font-mono">{typeof v === 'number' ? (v < 1 ? `${(v * 100).toFixed(1)}%` : v) : String(v)}</span>
                </div>
              ))}
              <div className="mt-2 pt-2 border-t border-border">
                <p className="text-muted mb-1">Active Filters</p>
                <div className="flex flex-wrap gap-1">
                  {Object.entries(pendingSettings).filter(([k, v]) => typeof v === 'boolean' && v && k.startsWith('use_')).map(([k]) => (
                    <span key={k} className="px-2 py-0.5 rounded bg-accent/15 text-accent text-xs">{k.replace('use_', '').replace('_filter', '').toUpperCase()}</span>
                  ))}
                </div>
              </div>
            </div>
            <div className="flex gap-3">
              <button onClick={() => setShowConfirm(false)} className="flex-1 py-2.5 rounded-xl border border-border text-muted hover:text-white text-sm transition-colors">Cancel</button>
              <button onClick={confirmSaveSettings} className="flex-1 py-2.5 rounded-xl bg-accent hover:bg-blue-500 text-sm font-semibold transition-colors">Confirm Save</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
