import React, { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
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
    A: 'bg-profit/20 text-profit',
    B: 'bg-accent/20 text-accent',
    C: 'bg-warning/20 text-warning',
    D: 'bg-orange-500/20 text-orange-400',
    F: 'bg-loss/20 text-loss',
  }
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full font-bold ${styles[grade] ?? 'bg-border text-muted'}`}>
      {grade}
    </span>
  )
}

function Toast({ msg, ok }: { msg: string; ok: boolean }) {
  return (
    <div
      className={`fixed bottom-6 right-6 z-50 px-5 py-3 rounded-xl shadow-xl text-sm font-medium slide-in ${ok ? 'bg-profit' : 'bg-loss'} text-white`}
    >
      {msg}
    </div>
  )
}

// ── Main Dashboard ────────────────────────────────────────────────────────────

export function Dashboard() {
  const { user, logout, refreshUser } = useAuth()
  const navigate = useNavigate()

  const [botStatus, setBotStatus] = useState<BotStatus | null>(null)
  const [livePrice, setLivePrice] = useState<number | null>(null)
  const [liveZ, setLiveZ] = useState<number | null>(null)
  const [trades, setTrades] = useState<Trade[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [report, setReport] = useState<DailyReport | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [expandedTrade, setExpandedTrade] = useState<string | null>(null)
  const [tradeFilter, setTradeFilter] = useState<'all' | 'live' | 'demo'>('all')
  const [feed, setFeed] = useState<LiveEvent[]>([])
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null)
  const [botLoading, setBotLoading] = useState(false)
  const [settingsLoading, setSettingsLoading] = useState(false)
  const [keysLoading, setKeysLoading] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [balance, setBalance] = useState<Balance | null>(null)
  const [anthropicKey, setAnthropicKey] = useState('')
  const [anthropicConfigured, setAnthropicConfigured] = useState(false)
  const [anthropicLoading, setAnthropicLoading] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [pendingSettings, setPendingSettings] = useState<null | {
    trading_symbol: string; entry_z: number; lookback: number
    stop_loss_pct: number; take_profit_pct: number; trail_stop_pct: number
  }>(null)

  const [testLoading, setTestLoading] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)

  // Settings form — research-backed optimal defaults for BTC Z-score retest strategy
  const [rhApiKey, setRhApiKey] = useState('')
  const [formSymbol, setFormSymbol] = useState('BTC-USD')
  const [formEntryZ, setFormEntryZ] = useState(2.0)    // 2σ boundary — statistically significant
  const [formLookback, setFormLookback] = useState(20) // Bollinger standard (20-period mean)
  const [formStopLoss, setFormStopLoss] = useState(0.025)    // 2.5% — BTC ATR buffer
  const [formTakeProfit, setFormTakeProfit] = useState(0.05) // 5.0% — 2:1 R/R ratio
  const [formTrailStop, setFormTrailStop] = useState(0.015)  // 1.5% — avoids micro-volatility exits

  const wsRef = useRef<WebSocket | null>(null)
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const wsAuthFailedRef = useRef(false)

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
      api.get('/api/bot/ai-status'),
    ])
    const [statusR, tradesR, statsR, reportR, settingsR, aiStatusR] = results
    if (statusR.status === 'fulfilled') setBotStatus(statusR.value.data)
    if (tradesR.status === 'fulfilled') setTrades(tradesR.value.data)
    if (statsR.status === 'fulfilled') setStats(statsR.value.data)
    if (reportR.status === 'fulfilled') setReport(reportR.value.data)
    if (settingsR.status === 'fulfilled') {
      const s: Settings = settingsR.value.data
      setSettings(s)
      setFormSymbol(s.trading_symbol)
      setFormEntryZ(s.entry_z)
      setFormLookback(parseInt(s.lookback))
      setFormStopLoss(s.stop_loss_pct)
      setFormTakeProfit(s.take_profit_pct)
      setFormTrailStop(s.trail_stop_pct)
      if (s.has_api_keys) {
        api.get('/api/bot/balance').then(r => setBalance(r.data)).catch(() => {})
      }
    }
    if (aiStatusR.status === 'fulfilled') {
      setAnthropicConfigured(aiStatusR.value.data.configured)
    }
  }, [tradeFilter])

  // WebSocket
  const connectWS = useCallback(() => {
    const token = getAccessToken()
    if (!token) return
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/ws?token=${token}`)
    wsRef.current = ws

    ws.onopen = () => addFeed('connected', 'WebSocket connected — live updates active', 'text-accent')

    ws.onmessage = ev => {
      try {
        const d = JSON.parse(ev.data)
        if (d.type === 'status_update') {
          // Only use bot price if live (not demo) — real market price poll is source of truth for header
          if (!d.demo_mode && d.price) setLivePrice(d.price)
          setLiveZ(d.z_score)
          setBotStatus(prev => prev
            ? { ...prev, running: true, in_trade: d.in_trade, entry_price: d.entry_price, trade_side: d.trade_side, trail_stop: d.trail_stop, last_signal: d.last_signal, demo_mode: d.demo_mode, ...(d.key_invalid === false ? { key_invalid: false } : {}) }
            : null)
        } else if (d.type === 'trade_opened') {
          addFeed('trade_opened', `${(d.side as string).toUpperCase()} ${d.symbol} @ $${(d.entry_price as number).toLocaleString()}${d.demo_mode ? ' [DEMO]' : ''}`, 'text-profit')
          loadData()
        } else if (d.type === 'trade_closed') {
          const pnl = (d.pnl as number) || 0
          addFeed('trade_closed', `Closed (${d.exit_reason}) · P&L: ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(4)}${d.demo_mode ? ' [DEMO]' : ''}`, pnl >= 0 ? 'text-profit' : 'text-loss')
          loadData()
        } else if (d.type === 'ai_analysis_ready') {
          addFeed('ai', `AI graded last trade: ${d.analysis?.grade ?? 'N/A'}`, 'text-purple-400')
          loadData()
        } else if (d.type === 'bot_error') {
          addFeed('error', `Error: ${d.message}`, 'text-loss')
        } else if (d.type === 'key_invalid') {
          addFeed('error', d.message, 'text-loss')
          // Reflect key_invalid in bot status so Start Live button disables
          setBotStatus(prev => prev ? { ...prev, key_invalid: true } : null)
          loadData()
        }
      } catch { /* ignore */ }
    }

    ws.onclose = (ev) => {
      if (ev.code === 4002) {
        // Token rejected — stop all reconnects, try refresh once
        if (reconnectRef.current) clearTimeout(reconnectRef.current)
        if (wsAuthFailedRef.current) return  // Already handling, don't loop
        wsAuthFailedRef.current = true
        api.post('/api/auth/refresh').then(() => {
          wsAuthFailedRef.current = false
          reconnectRef.current = setTimeout(connectWS, 500)
        }).catch(async () => {
          // Both tokens dead — force re-login
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

  // Always poll real market price — this is the authoritative source for the header ticker
  useEffect(() => {
    const symbol = settings?.trading_symbol ?? 'BTC-USD'
    let stale = false
    const fetchPrice = async () => {
      try {
        const res = await api.get(`/api/market/price?symbol=${symbol}`)
        if (res.data.price && !stale) setLivePrice(res.data.price)
        else if (!res.data.price) console.warn('Price API returned null')
      } catch (e) {
        console.warn('Market price fetch failed:', e)
      }
    }
    fetchPrice()
    const interval = setInterval(fetchPrice, 5000)
    return () => { stale = true; clearInterval(interval) }
  }, [settings?.trading_symbol])

  useEffect(() => {
    loadData()
    const t = setTimeout(connectWS, 400)
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
    } finally {
      setBotLoading(false)
    }
  }

  const stopBot = async () => {
    setBotLoading(true)
    try {
      await api.post('/api/bot/stop')
      await refreshUser()
      const r = await api.get('/api/bot/status')
      setBotStatus(r.data)
      showToast('Bot stopped')
    } catch {
      showToast('Failed to stop bot', false)
    } finally {
      setBotLoading(false)
    }
  }

  const saveKeys = async () => {
    if (!rhApiKey) return showToast('API key is required', false)
    setKeysLoading(true)
    try {
      const res = await api.post('/api/bot/keys', { rh_api_key: rhApiKey })
      await refreshUser()
      await loadData()
      api.get('/api/bot/balance').then(r => setBalance(r.data)).catch(() => {})
      const msg = res.data?.message ?? 'API key saved!'
      showToast(msg.includes('live') ? 'Switched to LIVE mode!' : 'API key saved!')
      setRhApiKey('')
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      showToast(detail ?? 'Failed to save keys', false)
    } finally {
      setKeysLoading(false)
    }
  }

  const requestSaveSettings = () => {
    setPendingSettings({
      trading_symbol: formSymbol, entry_z: formEntryZ, lookback: formLookback,
      stop_loss_pct: formStopLoss, take_profit_pct: formTakeProfit, trail_stop_pct: formTrailStop,
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
    } catch {
      showToast('Failed to save settings', false)
    } finally {
      setSettingsLoading(false)
      setPendingSettings(null)
    }
  }

  const saveAnthropicKey = async () => {
    if (!anthropicKey) return showToast('Anthropic API key is required', false)
    setAnthropicLoading(true)
    try {
      await api.post('/api/bot/anthropic-key', { anthropic_api_key: anthropicKey })
      setAnthropicConfigured(true)
      setAnthropicKey('')
      showToast('Anthropic key saved — AI analysis enabled!')
    } catch {
      showToast('Failed to save Anthropic key', false)
    } finally {
      setAnthropicLoading(false)
    }
  }

  const testConnection = async () => {
    setTestLoading(true)
    setTestResult(null)
    try {
      const res = await api.post('/api/bot/test-connection')
      if (res.data.ok) {
        setTestResult({ ok: true, msg: `Connected! Buying power: $${res.data.buying_power?.toFixed(2) ?? '0.00'}` })
        setBotStatus(prev => prev ? { ...prev, key_invalid: false } : null)
        loadData()
      } else {
        setTestResult({ ok: false, msg: res.data.error ?? 'Connection failed' })
      }
    } catch {
      setTestResult({ ok: false, msg: 'Request failed — check your connection' })
    } finally {
      setTestLoading(false)
    }
  }

  const handleLogout = async () => {
    await logout()
    navigate('/')
  }

  const isDemo = settings?.demo_mode ?? !user?.has_api_keys

  const currentPnl =
    botStatus?.in_trade && botStatus.entry_price && livePrice
      ? botStatus.trade_side === 'buy'
        ? ((livePrice - botStatus.entry_price) / botStatus.entry_price) * 100
        : ((botStatus.entry_price - livePrice) / botStatus.entry_price) * 100
      : null

  return (
    <div className="min-h-screen bg-dark text-white">
      {toast && <Toast msg={toast.msg} ok={toast.ok} />}

      {/* Top Bar */}
      <header className="sticky top-0 z-40 border-b border-border bg-dark/95 backdrop-blur-md">
        <div className="max-w-7xl mx-auto px-4 h-14 flex items-center justify-between gap-4">
          <div className="flex items-center gap-2 flex-shrink-0">
            <span className="text-xl">⚡</span>
            <span className="font-bold text-sm" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>
              CryptoBot
            </span>
            {isDemo && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-warning/20 text-warning border border-warning/30 font-medium">
                DEMO
              </span>
            )}
          </div>

          {/* Live ticker */}
          <div className="flex items-center gap-3 text-sm font-mono">
            <span className="text-muted text-xs hidden sm:block">{settings?.trading_symbol ?? 'BTC-USD'}</span>
            {livePrice ? (
              <span className="text-white font-semibold">
                ${livePrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </span>
            ) : (
              <span className="text-muted text-xs animate-pulse">fetching…</span>
            )}
            {liveZ !== null && (
              <span className={`text-xs px-2 py-0.5 rounded-full hidden sm:inline ${Math.abs(liveZ) > 1.5 ? 'bg-warning/20 text-warning' : 'bg-border text-muted'}`}>
                Z={liveZ.toFixed(2)}
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            <span className="text-xs text-muted hidden md:block">{user?.email}</span>
            <button
              onClick={() => setShowSettings(s => !s)}
              className="text-muted hover:text-white transition-colors p-1.5 rounded-lg hover:bg-elevated"
              title="Settings"
            >
              ⚙️
            </button>
            <button
              onClick={handleLogout}
              className="text-xs px-3 py-1.5 rounded-lg border border-border text-muted hover:text-white hover:border-accent/50 transition-colors"
            >
              Logout
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 py-6 space-y-5">

        {/* Demo Banner */}
        {isDemo && (
          <div className="px-5 py-3 rounded-xl bg-warning/10 border border-warning/30 flex items-start gap-3">
            <span className="text-xl mt-0.5">🎭</span>
            <div>
              <p className="text-sm font-semibold text-warning">Demo Mode Active</p>
              <p className="text-xs text-muted mt-0.5">
                The bot is running with simulated prices — no real orders are placed.
                Add your Robinhood API keys in Settings to trade for real.
              </p>
            </div>
          </div>
        )}

        {/* ── Invalid Key Warning ── */}
        {botStatus?.key_invalid && (
          <div className="flex items-start gap-3 p-4 rounded-2xl bg-loss/10 border border-loss/30">
            <span className="text-loss text-lg flex-shrink-0">⚠</span>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-loss">Robinhood API Key Invalid</p>
              <p className="text-xs text-muted mt-0.5">
                Your key was rejected (401). Possible causes: wrong key pasted, key was revoked on Robinhood, or the public key registered on Robinhood doesn't match.
                Open Settings, paste your Robinhood API key again, and click <strong>Test Connection</strong> to verify.
              </p>
            </div>
            <button onClick={() => setShowSettings(true)} className="flex-shrink-0 text-xs px-3 py-1.5 rounded-lg bg-loss/20 border border-loss/30 text-loss hover:bg-loss/30 transition-colors font-medium">
              Open Settings →
            </button>
          </div>
        )}

        {/* ── Bot Control ── */}
        <div className="bg-card border border-border rounded-2xl p-5">
          <div className="flex flex-col lg:flex-row lg:items-center gap-5">
            {/* Status + Buttons */}
            <div className="flex items-center gap-4 flex-shrink-0">
              <div className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${botStatus?.running ? 'pulse-dot bg-profit' : 'bg-muted'}`} />
              <div>
                <span className="font-semibold text-sm">
                  {botStatus?.running
                    ? (botStatus.demo_mode ? 'Running — Demo' : 'Running — Live')
                    : 'Bot is stopped'}
                </span>
                {botStatus?.last_signal && (
                  <p className="text-xs text-muted mt-0.5 truncate max-w-xs">{botStatus.last_signal}</p>
                )}
              </div>
              <div className="flex items-center gap-2 ml-2">
                {botStatus?.running ? (
                  <button
                    onClick={stopBot}
                    disabled={botLoading}
                    className="px-4 py-1.5 rounded-lg bg-loss/20 border border-loss/40 text-loss text-xs font-semibold hover:bg-loss/30 transition-colors disabled:opacity-50"
                  >
                    {botLoading ? '…' : 'Stop'}
                  </button>
                ) : (
                  <>
                    <button
                      onClick={() => startBot('demo')}
                      disabled={botLoading}
                      className="px-4 py-1.5 rounded-lg bg-warning/15 border border-warning/40 text-warning text-xs font-semibold hover:bg-warning/25 transition-colors disabled:opacity-50"
                    >
                      {botLoading ? '…' : 'Start Demo'}
                    </button>
                    <button
                      onClick={() => startBot('live')}
                      disabled={botLoading || !settings?.has_api_keys || botStatus?.key_invalid}
                      title={
                        !settings?.has_api_keys ? 'Add your Robinhood API key first' :
                        botStatus?.key_invalid ? 'API key is invalid — update it in Settings' :
                        'Start live trading'
                      }
                      className="px-4 py-1.5 rounded-lg bg-profit/15 border border-profit/40 text-profit text-xs font-semibold hover:bg-profit/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {botLoading ? '…' : 'Start Live'}
                    </button>
                  </>
                )}
              </div>
            </div>

            {/* Active trade details */}
            {botStatus?.in_trade && botStatus.entry_price && (
              <div className="flex-1 p-4 rounded-xl bg-elevated border border-border">
                <div className="flex flex-wrap gap-4 items-center">
                  {[
                    { label: 'Symbol', value: settings?.trading_symbol ?? '—' },
                    { label: 'Side', badge: true, val: botStatus.trade_side },
                    { label: 'Entry', value: `$${botStatus.entry_price.toLocaleString()}` },
                    ...(livePrice ? [{ label: 'Current', value: `$${livePrice.toLocaleString('en-US', { minimumFractionDigits: 2 })}` }] : []),
                    ...(currentPnl !== null ? [{ label: 'Live P&L', pnl: true, val: currentPnl }] : []),
                    ...(botStatus.trail_stop ? [{ label: 'Trail Stop', value: `$${botStatus.trail_stop.toFixed(2)}`, dimmed: true }] : []),
                  ].map((item, i) => (
                    <div key={i}>
                      <p className="text-xs text-muted">{item.label}</p>
                      {'badge' in item && item.badge ? (
                        <span className={`text-xs font-bold px-2 py-0.5 rounded ${item.val === 'buy' ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'}`}>
                          {(item.val as string).toUpperCase()}
                        </span>
                      ) : 'pnl' in item && item.pnl ? (
                        <p className={`font-mono font-bold text-sm ${(item.val as number) >= 0 ? 'text-profit' : 'text-loss'}`}>
                          {(item.val as number) >= 0 ? '+' : ''}{(item.val as number).toFixed(2)}%
                        </p>
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

        {/* ── Stats + Chart ── */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          <div className="bg-card border border-border rounded-2xl p-5">
            <h2 className="text-xs font-semibold text-muted mb-4 uppercase tracking-wider">Performance</h2>
            <div className="grid grid-cols-2 gap-3">
              {[
                { label: 'Total Trades', val: stats?.total ?? '—' },
                { label: 'Win Rate', val: stats ? `${stats.win_rate}%` : '—', color: stats && stats.win_rate >= 50 ? 'text-profit' : 'text-loss' },
                { label: 'Total P&L', val: stats ? `$${stats.total_pnl.toFixed(4)}` : '—', color: stats && stats.total_pnl >= 0 ? 'text-profit' : 'text-loss' },
                { label: 'Open', val: trades.filter(t => t.state === 'open').length },
              ].map((s, i) => (
                <div key={i} className="p-3 rounded-xl bg-elevated">
                  <p className="text-xs text-muted mb-1">{s.label}</p>
                  <p className={`text-xl font-bold font-mono ${s.color ?? 'text-white'}`}>{s.val}</p>
                </div>
              ))}
            </div>

            {/* Robinhood Balance */}
            {balance && !balance.error && (
              <div className="mt-4 pt-4 border-t border-border">
                <p className="text-xs font-semibold text-muted mb-2 uppercase tracking-wider">Robinhood Balance</p>
                {balance.available !== null && (
                  <div className="p-3 rounded-xl bg-elevated mb-2">
                    <p className="text-xs text-muted mb-1">Buying Power</p>
                    <p className="text-xl font-bold font-mono text-profit">
                      ${balance.available.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </p>
                  </div>
                )}
                {balance.holdings.length > 0 && (
                  <div className="space-y-1.5">
                    {balance.holdings.map((h, i) => (
                      <div key={i} className="flex justify-between items-center px-3 py-2 rounded-xl bg-elevated text-xs">
                        <span className="font-semibold">{h.asset_code}</span>
                        <span className="font-mono text-muted">{parseFloat(h.total_quantity).toFixed(6)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
            {balance?.error && balance.error !== 'No API keys configured' && (
              <div className="mt-3 px-3 py-2 rounded-xl bg-loss/10 border border-loss/30">
                <p className="text-xs text-loss font-medium">Balance unavailable</p>
                <p className="text-xs text-muted mt-0.5">{balance.error}</p>
                <button onClick={() => setShowSettings(true)} className="text-xs text-accent mt-1 hover:underline">Open Settings →</button>
              </div>
            )}
          </div>

          <div className="lg:col-span-2 bg-card border border-border rounded-2xl p-5">
            <h2 className="text-xs font-semibold text-muted mb-4 uppercase tracking-wider">P&L — Last 30 Trades</h2>
            {stats?.pnl_chart?.length ? (
              <ResponsiveContainer width="100%" height={155}>
                <AreaChart data={stats.pnl_chart}>
                  <defs>
                    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#10B981" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#10B981" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#A0A3B1' }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fontSize: 10, fill: '#A0A3B1' }} axisLine={false} tickLine={false} width={48} />
                  <Tooltip
                    contentStyle={{ backgroundColor: '#1A1B23', border: '1px solid #2A2B35', borderRadius: 8, fontSize: 12 }}
                    labelStyle={{ color: '#A0A3B1' }}
                    itemStyle={{ color: '#10B981' }}
                  />
                  <Area type="monotone" dataKey="pnl" stroke="#10B981" fill="url(#g)" strokeWidth={2} dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="h-36 flex items-center justify-center text-muted text-sm">
                Start the bot to see P&L data here.
              </div>
            )}
          </div>
        </div>

        {/* ── Trade History ── */}
        <div className="bg-card border border-border rounded-2xl p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xs font-semibold text-muted uppercase tracking-wider">Trade History</h2>
            <div className="flex gap-1 bg-elevated rounded-lg p-0.5">
              {(['all', 'live', 'demo'] as const).map(f => (
                <button
                  key={f}
                  onClick={() => setTradeFilter(f)}
                  className={`px-3 py-1 rounded-md text-xs font-semibold transition-colors ${
                    tradeFilter === f
                      ? f === 'live' ? 'bg-profit/20 text-profit' : f === 'demo' ? 'bg-warning/20 text-warning' : 'bg-accent/20 text-accent'
                      : 'text-muted hover:text-white'
                  }`}
                >
                  {f === 'all' ? 'All' : f === 'live' ? 'Live' : 'Demo'}
                </button>
              ))}
            </div>
          </div>
          {trades.length === 0 ? (
            <p className="text-muted text-sm text-center py-10">
              No trades yet — start the bot and trades will appear here automatically.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-muted border-b border-border">
                    {['Date', 'Symbol', 'Mode', 'Side', 'Entry', 'Exit', 'P&L', 'Reason', 'AI'].map((h, i) => (
                      <th key={i} className={`pb-3 font-medium ${i >= 4 ? 'text-right' : i === 8 ? 'text-center' : 'text-left'}`}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {trades.map(trade => (
                    <React.Fragment key={trade.id}>
                      <tr
                        onClick={() => setExpandedTrade(expandedTrade === trade.id ? null : trade.id)}
                        className="border-b border-border/40 hover:bg-elevated/50 cursor-pointer transition-colors"
                      >
                        <td className="py-3 text-muted text-xs font-mono">
                          {trade.opened_at ? new Date(trade.opened_at).toLocaleDateString() : '—'}
                        </td>
                        <td className="py-3 font-mono font-medium">{trade.symbol}</td>
                        <td className="py-3">
                          <span className={`text-xs px-1.5 py-0.5 rounded font-semibold ${trade.is_demo ? 'bg-warning/15 text-warning' : 'bg-profit/15 text-profit'}`}>
                            {trade.is_demo ? 'DEMO' : 'LIVE'}
                          </span>
                        </td>
                        <td className="py-3">
                          <span className={`text-xs px-2 py-0.5 rounded font-bold ${trade.side === 'buy' ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'}`}>
                            {trade.side.toUpperCase()}
                          </span>
                        </td>
                        <td className="py-3 text-right font-mono text-xs">
                          {trade.entry_price ? `$${parseFloat(trade.entry_price).toLocaleString()}` : '—'}
                        </td>
                        <td className="py-3 text-right font-mono text-xs">
                          {trade.exit_price ? `$${parseFloat(trade.exit_price).toLocaleString()}` : '—'}
                        </td>
                        <td className="py-3 text-right font-mono">
                          {trade.pnl !== null ? (
                            <span className={trade.pnl >= 0 ? 'text-profit' : 'text-loss'}>
                              {trade.pnl >= 0 ? '+' : ''}${trade.pnl.toFixed(4)}
                            </span>
                          ) : (
                            <span className="text-xs text-muted">{trade.state === 'open' ? 'OPEN' : '—'}</span>
                          )}
                        </td>
                        <td className="py-3 text-xs text-muted">{trade.exit_reason?.replace(/_/g, ' ') ?? '—'}</td>
                        <td className="py-3 text-right">
                          <GradeBadge grade={trade.ai?.grade} />
                        </td>
                      </tr>

                      {expandedTrade === trade.id && trade.ai && (
                        <tr key={`${trade.id}-ai`}>
                          <td colSpan={8} className="px-4 py-4 bg-elevated/40">
                            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
                              <div>
                                <p className="text-muted font-semibold mb-1">Entry Quality</p>
                                <p>{trade.ai.entry_quality}</p>
                              </div>
                              <div>
                                <p className="text-muted font-semibold mb-1">Exit Quality</p>
                                <p>{trade.ai.exit_quality}</p>
                              </div>
                              <div>
                                <p className="text-muted font-semibold mb-1">Confidence</p>
                                <p>{(trade.ai.confidence * 100).toFixed(0)}%</p>
                              </div>
                              {trade.ai.what_went_well.length > 0 && (
                                <div>
                                  <p className="text-profit font-semibold mb-1">✓ What went well</p>
                                  <ul className="space-y-0.5">{trade.ai.what_went_well.map((w, i) => <li key={i}>• {w}</li>)}</ul>
                                </div>
                              )}
                              {trade.ai.what_went_wrong.length > 0 && (
                                <div>
                                  <p className="text-loss font-semibold mb-1">✗ What went wrong</p>
                                  <ul className="space-y-0.5">{trade.ai.what_went_wrong.map((w, i) => <li key={i}>• {w}</li>)}</ul>
                                </div>
                              )}
                              {trade.ai.improvements.length > 0 && (
                                <div>
                                  <p className="text-warning font-semibold mb-1">⚡ Improvements</p>
                                  <ul className="space-y-0.5">{trade.ai.improvements.map((w, i) => <li key={i}>• {w}</li>)}</ul>
                                </div>
                              )}
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

        {/* ── AI Report + Live Feed ── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">

          {/* AI Report */}
          <div className="bg-card border border-border rounded-2xl p-5">
            <h2 className="text-xs font-semibold text-muted mb-4 uppercase tracking-wider">Latest AI Report</h2>
            {report ? (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-muted">{report.report_date}</span>
                  <div className="flex gap-2">
                    {report.full_report?.recommendation && (
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium capitalize ${
                        report.full_report.recommendation === 'continue' ? 'bg-profit/20 text-profit' :
                        report.full_report.recommendation === 'pause' ? 'bg-loss/20 text-loss' :
                        'bg-warning/20 text-warning'
                      }`}>
                        {report.full_report.recommendation.replace(/_/g, ' ')}
                      </span>
                    )}
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-2 text-center">
                  {[
                    { label: 'Win Rate', val: `${report.win_rate.toFixed(1)}%`, color: report.win_rate >= 50 ? 'text-profit' : 'text-loss' },
                    { label: 'Trades', val: report.total_trades, color: 'text-white' },
                    { label: 'P&L', val: `${report.total_pnl >= 0 ? '+' : ''}$${report.total_pnl.toFixed(4)}`, color: report.total_pnl >= 0 ? 'text-profit' : 'text-loss' },
                  ].map((s, i) => (
                    <div key={i} className="p-2 rounded-lg bg-elevated">
                      <p className="text-xs text-muted">{s.label}</p>
                      <p className={`font-bold font-mono text-sm ${s.color}`}>{s.val}</p>
                    </div>
                  ))}
                </div>
                {report.summary && <p className="text-sm text-muted leading-relaxed">{report.summary}</p>}
                {report.top_improvement && (
                  <div className="px-4 py-3 rounded-xl bg-warning/10 border border-warning/20">
                    <p className="text-xs text-warning font-semibold mb-1">⚡ Top Improvement</p>
                    <p className="text-sm">{report.top_improvement}</p>
                  </div>
                )}
                {(report.full_report?.patterns_noticed?.length ?? 0) > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {report.full_report.patterns_noticed!.map((p, i) => (
                      <span key={i} className="text-xs px-2 py-1 rounded-full bg-elevated border border-border text-muted">{p}</span>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <p className="text-muted text-sm text-center py-10">
                Reports are generated automatically each day after trading activity.
              </p>
            )}
          </div>

          {/* Live Feed */}
          <div className="bg-card border border-border rounded-2xl p-5">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xs font-semibold text-muted uppercase tracking-wider">Live Feed</h2>
              <div className="flex items-center gap-1.5">
                <div className="pulse-dot w-1.5 h-1.5 rounded-full bg-profit" />
                <span className="text-xs text-muted">Live</span>
              </div>
            </div>
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {feed.length === 0 ? (
                <p className="text-muted text-sm text-center py-10">Waiting for events…</p>
              ) : (
                feed.map(ev => (
                  <div key={ev.id} className="slide-in flex items-start gap-3 p-3 rounded-xl bg-elevated border border-border/50">
                    <span className={`text-sm flex-shrink-0 ${ev.color}`}>
                      {ev.type === 'trade_opened' ? '📈' : ev.type === 'trade_closed' ? '📉' : ev.type === 'ai' ? '🧠' : ev.type === 'error' ? '⚠️' : ev.type === 'connected' ? '🔌' : '·'}
                    </span>
                    <div className="flex-1 min-w-0">
                      <p className={`text-xs ${ev.color} break-words`}>{ev.message}</p>
                      <p className="text-xs text-muted/60 mt-0.5">{ev.time}</p>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>

        {/* ── Settings Confirmation Modal ── */}
        {showConfirm && pendingSettings && (
          <div className="fixed inset-0 z-[60] flex items-center justify-center p-4" onClick={() => setShowConfirm(false)}>
            <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />
            <div className="relative bg-card border border-border rounded-2xl p-6 w-full max-w-sm shadow-2xl" onClick={e => e.stopPropagation()}>
              <div className="flex items-center gap-3 mb-4">
                <span className="text-2xl">⚠️</span>
                <div>
                  <h3 className="font-semibold text-sm">Confirm Settings Change</h3>
                  <p className="text-xs text-muted mt-0.5">These will apply on the bot's next trade</p>
                </div>
              </div>
              <div className="space-y-1.5 mb-5 text-xs">
                {[
                  { label: 'Symbol', val: pendingSettings.trading_symbol },
                  { label: 'Entry Z-Score', val: pendingSettings.entry_z.toFixed(1) },
                  { label: 'Lookback', val: `${pendingSettings.lookback} periods` },
                  { label: 'Stop Loss', val: `${(pendingSettings.stop_loss_pct * 100).toFixed(1)}%` },
                  { label: 'Take Profit', val: `${(pendingSettings.take_profit_pct * 100).toFixed(1)}%` },
                  { label: 'Trail Stop', val: `${(pendingSettings.trail_stop_pct * 100).toFixed(1)}%` },
                ].map((row, i) => (
                  <div key={i} className="flex justify-between px-3 py-1.5 rounded-lg bg-elevated">
                    <span className="text-muted">{row.label}</span>
                    <span className="font-semibold font-mono">{row.val}</span>
                  </div>
                ))}
              </div>
              <div className="flex gap-3">
                <button onClick={() => setShowConfirm(false)} className="flex-1 py-2.5 rounded-xl border border-border text-muted hover:text-white text-sm transition-colors">
                  Cancel
                </button>
                <button onClick={confirmSaveSettings} className="flex-1 py-2.5 rounded-xl bg-accent hover:bg-blue-500 text-sm font-semibold transition-colors">
                  Confirm Save
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ── Settings Modal ── */}
        {showSettings && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4" onClick={() => setShowSettings(false)}>
            <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
            <div className="relative bg-card border border-border rounded-2xl p-5 w-full max-w-3xl max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xs font-semibold text-muted uppercase tracking-wider">Settings</h2>
              <button onClick={() => setShowSettings(false)} className="text-muted hover:text-white text-xl leading-none">×</button>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">

              {/* API Keys */}
              <div>
                <h3 className="font-semibold mb-4 text-sm flex items-center gap-2">
                  Robinhood API Setup
                  {user?.has_api_keys && <span className="text-xs text-profit">✓ configured</span>}
                </h3>
                <div className="space-y-4">
                  {/* Public key — copy and register on Robinhood */}
                  <div>
                    <label className="block text-xs text-muted mb-1.5">
                      Your Public Key <span className="text-muted/60">(register this on Robinhood → Account → Crypto API)</span>
                    </label>
                    <div className="flex gap-2">
                      <input
                        type="text"
                        readOnly
                        value={settings?.public_key ?? ''}
                        className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-xs font-mono focus:outline-none select-all cursor-text"
                      />
                      <button
                        onClick={() => { navigator.clipboard.writeText(settings?.public_key ?? ''); showToast('Public key copied!') }}
                        className="flex-shrink-0 px-3 py-2.5 rounded-xl bg-elevated border border-border text-muted hover:text-white hover:border-accent/50 transition-colors text-xs"
                        title="Copy public key"
                      >
                        Copy
                      </button>
                    </div>
                  </div>
                  {/* Robinhood API key */}
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Robinhood API Key <span className="text-muted/60">(from Robinhood after registering your public key)</span></label>
                    <input
                      type="text"
                      value={rhApiKey}
                      onChange={e => setRhApiKey(e.target.value)}
                      placeholder="rh-api-key-..."
                      className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent"
                    />
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={saveKeys}
                      disabled={keysLoading}
                      className="px-5 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 transition-colors text-sm font-medium"
                    >
                      {keysLoading ? 'Saving…' : 'Save API Key'}
                    </button>
                    {settings?.has_api_keys && (
                      <button
                        onClick={testConnection}
                        disabled={testLoading}
                        className="px-4 py-2.5 rounded-xl bg-elevated border border-border hover:border-accent/50 disabled:opacity-50 transition-colors text-sm font-medium text-muted hover:text-white"
                      >
                        {testLoading ? 'Testing…' : 'Test Connection'}
                      </button>
                    )}
                  </div>
                  {testResult && (
                    <div className={`mt-2 px-3 py-2 rounded-lg text-xs ${testResult.ok ? 'bg-profit/10 border border-profit/30 text-profit' : 'bg-loss/10 border border-loss/30 text-loss'}`}>
                      {testResult.ok ? '✓ ' : '✗ '}{testResult.msg}
                    </div>
                  )}
                </div>
              </div>

              {/* AI Learning */}
              <div className="lg:col-span-2 border-t border-border pt-6">
                <h3 className="font-semibold mb-1 text-sm flex items-center gap-2">
                  AI Learning System
                  {anthropicConfigured
                    ? <span className="text-xs text-profit">✓ enabled</span>
                    : <span className="text-xs text-warning">⚠ not configured</span>}
                </h3>
                <p className="text-xs text-muted mb-4">
                  Powers per-trade grading (A–F), entry/exit quality analysis, and daily performance reports.
                  Get your key at <span className="text-accent">console.anthropic.com</span>.
                </p>
                <div className="flex gap-2">
                  <input
                    type="password"
                    value={anthropicKey}
                    onChange={e => setAnthropicKey(e.target.value)}
                    placeholder={anthropicConfigured ? '••••••••••••••••••••••••• (already set)' : 'sk-ant-...'}
                    className="flex-1 px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent"
                  />
                  <button
                    onClick={saveAnthropicKey}
                    disabled={anthropicLoading}
                    className="px-5 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 transition-colors text-sm font-medium flex-shrink-0"
                  >
                    {anthropicLoading ? 'Saving…' : anthropicConfigured ? 'Update Key' : 'Enable AI'}
                  </button>
                </div>
              </div>

              {/* Bot Parameters */}
              <div>
                <h3 className="font-semibold mb-4 text-sm">Bot Parameters</h3>
                <div className="space-y-4">
                  <div>
                    <label className="block text-xs text-muted mb-1.5">Trading Symbol</label>
                    <select
                      value={formSymbol}
                      onChange={e => setFormSymbol(e.target.value)}
                      className="w-full px-3 py-2.5 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent"
                    >
                      {['BTC-USD', 'ETH-USD', 'SOL-USD', 'DOGE-USD'].map(s => (
                        <option key={s} value={s}>{s}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-muted mb-1.5">
                      Entry Z-Score: <span className="text-white">{formEntryZ.toFixed(1)}</span>
                    </label>
                    <input
                      type="range" min={1} max={3} step={0.1}
                      value={formEntryZ}
                      onChange={e => setFormEntryZ(parseFloat(e.target.value))}
                      className="w-full accent-accent"
                    />
                    <div className="flex justify-between text-xs text-muted mt-1"><span>1.0</span><span>3.0</span></div>
                  </div>
                  <div className="grid grid-cols-3 gap-3">
                    {[
                      { label: 'Stop Loss %', val: formStopLoss, set: setFormStopLoss, step: 0.005, min: 0.005, max: 0.1 },
                      { label: 'Take Profit %', val: formTakeProfit, set: setFormTakeProfit, step: 0.005, min: 0.005, max: 0.2 },
                      { label: 'Trail Stop %', val: formTrailStop, set: setFormTrailStop, step: 0.005, min: 0.005, max: 0.05 },
                    ].map((f, i) => (
                      <div key={i}>
                        <label className="block text-xs text-muted mb-1.5">{f.label}</label>
                        <input
                          type="number" step={f.step} min={f.min} max={f.max}
                          value={f.val}
                          onChange={e => f.set(parseFloat(e.target.value))}
                          className="w-full px-3 py-2 rounded-xl bg-elevated border border-border text-white text-sm focus:outline-none focus:border-accent"
                        />
                      </div>
                    ))}
                  </div>
                  <button
                    onClick={requestSaveSettings}
                    disabled={settingsLoading}
                    className="px-5 py-2.5 rounded-xl bg-accent hover:bg-blue-500 disabled:opacity-50 transition-colors text-sm font-medium"
                  >
                    {settingsLoading ? 'Saving…' : 'Save Settings'}
                  </button>
                </div>
              </div>
            </div>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}
