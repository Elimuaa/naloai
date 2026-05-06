import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { api } from '../api/axios'

interface UserRow {
  id: string
  email: string
  signed_up: string | null
  has_api_keys: boolean
  bot_active: boolean
  trading_symbol: string
  demo_balance: number
  cash_balance: number
  open_position_value: number
  total_trades: number
  open_trades: number
  wins: number
  losses: number
  win_rate: number
  total_pnl: number
  daily_pnl: number
  weekly_pnl: number
  monthly_pnl: number
  is_premium: boolean
  premium_since: string | null
  calibration_count: number
}

interface Summary {
  total_users: number
  active_bots: number
  premium_users: number
  total_trades: number
  closed_trades: number
  platform_pnl: number
  monthly_premium_revenue: number
}

interface PeriodStats {
  trades: number
  wins: number
  losses: number
  win_rate_pct: number
  total_pnl: number
  demo_pnl: number
  live_pnl: number
  partial_pnl_locked: number
  avg_pnl_per_trade: number
  btc_hodl_pct: number
}

interface TodayStats {
  as_of_utc: string
  active_bots: number
  open_positions: number
  trades_opened_today: number
  today: PeriodStats
  last_7_days: PeriodStats
  all_time: PeriodStats
}

export function AdminDashboard() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [users, setUsers] = useState<UserRow[]>([])
  const [summary, setSummary] = useState<Summary | null>(null)
  const [todayStats, setTodayStats] = useState<TodayStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [toggleLoading, setToggleLoading] = useState<string | null>(null)
  const [search, setSearch] = useState('')

  const loadData = useCallback(async () => {
    try {
      const [usersRes, summaryRes, todayRes] = await Promise.all([
        api.get('/api/admin/users'),
        api.get('/api/admin/summary'),
        api.get('/api/admin/today-stats').catch(() => null),
      ])
      setUsers(usersRes.data.users || [])
      setSummary(summaryRes.data)
      if (todayRes) setTodayStats(todayRes.data)
    } catch (err) {
      console.error('Admin load failed:', err)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!user?.is_admin) {
      navigate('/dashboard')
      return
    }
    loadData()
    const interval = setInterval(loadData, 30000)
    return () => clearInterval(interval)
  }, [user, navigate, loadData])

  const togglePremium = async (userId: string) => {
    setToggleLoading(userId)
    try {
      await api.post(`/api/admin/users/${userId}/premium`)
      await loadData()
    } catch { /* ignore */ }
    finally { setToggleLoading(null) }
  }

  const handleLogout = async () => { await logout(); navigate('/') }

  const filteredUsers = users.filter(u =>
    u.email.toLowerCase().includes(search.toLowerCase())
  )

  const formatDate = (d: string | null) => {
    if (!d) return '\u2014'
    return new Date(d).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
  }

  const formatTime = (d: string | null) => {
    if (!d) return ''
    return new Date(d).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
  }

  if (loading) {
    return (
      <div className="min-h-screen bg-dark flex items-center justify-center">
        <div className="text-center">
          <div className="w-10 h-10 border-2 border-accent border-t-transparent rounded-full animate-spin mx-auto mb-4"></div>
          <p className="text-muted text-sm">Loading admin panel...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-dark text-white">
      {/* Header */}
      <header className="sticky top-0 z-40 border-b border-border bg-dark/95 backdrop-blur-md">
        <div className="max-w-7xl mx-auto px-4 h-14 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-xl">&#9889;</span>
            <span className="font-bold text-sm" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>Nalo.Ai</span>
            <span className="text-xs px-2.5 py-1 rounded-full bg-purple-500/20 text-purple-400 border border-purple-500/30 font-bold tracking-wide">ADMIN</span>
          </div>
          <div className="flex items-center gap-4">
            <button onClick={() => navigate('/dashboard')} className="text-xs px-3 py-1.5 rounded-lg border border-border text-muted hover:text-white hover:border-accent/50 transition-colors">My Dashboard</button>
            <span className="text-xs text-muted">{user?.email}</span>
            <button onClick={handleLogout} className="text-xs px-3 py-1.5 rounded-lg border border-border text-muted hover:text-white hover:border-accent/50 transition-colors">Logout</button>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 py-6 space-y-6">

        {/* Summary Cards */}
        {summary && (
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4">
            {[
              { label: 'Total Users', value: summary.total_users, icon: '&#x1f465;', color: 'text-white' },
              { label: 'Active Bots', value: summary.active_bots, icon: '&#x1f916;', color: 'text-profit' },
              { label: 'Pro Users', value: summary.premium_users, icon: '&#x1f451;', color: 'text-purple-400' },
              { label: 'Total Trades', value: summary.total_trades, icon: '&#x1f4ca;', color: 'text-accent' },
              { label: 'Closed Trades', value: summary.closed_trades, icon: '&#x2705;', color: 'text-muted' },
              { label: 'Platform P&L', value: `$${(summary.platform_pnl ?? 0).toFixed(2)}`, icon: '&#x1f4b0;', color: (summary.platform_pnl ?? 0) >= 0 ? 'text-profit' : 'text-loss' },
              { label: 'Monthly Revenue', value: `$${(summary.monthly_premium_revenue ?? 0).toFixed(2)}`, icon: '&#x1f4b5;', color: 'text-profit' },
            ].map((card, i) => (
              <div key={i} className="bg-card border border-border rounded-2xl p-4">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-lg" dangerouslySetInnerHTML={{ __html: card.icon }} />
                  <span className="text-xs text-muted font-medium">{card.label}</span>
                </div>
                <p className={`text-2xl font-bold font-mono ${card.color}`}>{card.value}</p>
              </div>
            ))}
          </div>
        )}

        {/* Live Performance Panels */}
        {todayStats && (
          <div className="space-y-4">
            {/* Live activity strip */}
            <div className="bg-gradient-to-r from-accent/10 via-purple-500/10 to-profit/10 border border-accent/30 rounded-2xl p-5">
              <div className="flex items-center justify-between mb-3">
                <h2 className="font-bold text-sm flex items-center gap-2" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>
                  <span className="w-2 h-2 rounded-full bg-profit animate-pulse"></span>
                  Live Platform Pulse
                </h2>
                <span className="text-xs text-muted font-mono">{new Date(todayStats.as_of_utc).toLocaleString()}</span>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div>
                  <p className="text-xs text-muted mb-1">Active Bots</p>
                  <p className="text-2xl font-bold font-mono text-profit">{todayStats.active_bots}</p>
                </div>
                <div>
                  <p className="text-xs text-muted mb-1">Open Positions</p>
                  <p className="text-2xl font-bold font-mono text-warning">{todayStats.open_positions}</p>
                </div>
                <div>
                  <p className="text-xs text-muted mb-1">Trades Opened Today</p>
                  <p className="text-2xl font-bold font-mono text-accent">{todayStats.trades_opened_today}</p>
                </div>
                <div>
                  <p className="text-xs text-muted mb-1">Trades Closed Today</p>
                  <p className="text-2xl font-bold font-mono">{todayStats.today.trades}</p>
                </div>
              </div>
            </div>

            {/* Period stats: Today / 7d / All-time */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {[
                { label: 'Today', stats: todayStats.today, accent: 'text-profit', border: 'border-profit/30' },
                { label: 'Last 7 Days', stats: todayStats.last_7_days, accent: 'text-accent', border: 'border-accent/30' },
                { label: 'All-Time', stats: todayStats.all_time, accent: 'text-purple-400', border: 'border-purple-500/30' },
              ].map(({ label, stats, accent, border }) => (
                <div key={label} className={`bg-card border ${border} rounded-2xl p-5`}>
                  <h3 className={`text-xs font-bold uppercase tracking-wider mb-4 ${accent}`}>{label}</h3>
                  <div className="space-y-3">
                    <div className="flex items-baseline justify-between">
                      <span className="text-xs text-muted">Total P&L</span>
                      <span className={`text-2xl font-bold font-mono ${stats.total_pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                        {stats.total_pnl >= 0 ? '+' : ''}${stats.total_pnl.toFixed(2)}
                      </span>
                    </div>
                    <div className="grid grid-cols-2 gap-2 pt-2 border-t border-border">
                      <div>
                        <p className="text-xs text-muted">Demo P&L</p>
                        <p className={`text-sm font-mono font-semibold ${stats.demo_pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                          {stats.demo_pnl >= 0 ? '+' : ''}${stats.demo_pnl.toFixed(2)}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-muted">Live P&L</p>
                        <p className={`text-sm font-mono font-semibold ${stats.live_pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                          {stats.live_pnl >= 0 ? '+' : ''}${stats.live_pnl.toFixed(2)}
                        </p>
                      </div>
                    </div>
                    <div className="grid grid-cols-3 gap-2 pt-2 border-t border-border text-center">
                      <div>
                        <p className="text-xs text-muted">Trades</p>
                        <p className="text-sm font-mono font-semibold">{stats.trades}</p>
                      </div>
                      <div>
                        <p className="text-xs text-muted">Win Rate</p>
                        <p className={`text-sm font-mono font-semibold ${stats.win_rate_pct >= 50 ? 'text-profit' : stats.win_rate_pct > 0 ? 'text-loss' : 'text-muted'}`}>
                          {stats.trades > 0 ? `${stats.win_rate_pct}%` : '\u2014'}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-muted">Avg/Trade</p>
                        <p className={`text-sm font-mono font-semibold ${stats.avg_pnl_per_trade >= 0 ? 'text-profit' : 'text-loss'}`}>
                          {stats.trades > 0 ? `${stats.avg_pnl_per_trade >= 0 ? '+' : ''}$${stats.avg_pnl_per_trade.toFixed(2)}` : '\u2014'}
                        </p>
                      </div>
                    </div>
                    <div className="grid grid-cols-2 gap-2 pt-2 border-t border-border text-center">
                      <div>
                        <p className="text-xs text-muted">W / L</p>
                        <p className="text-sm font-mono">
                          <span className="text-profit">{stats.wins}</span>
                          <span className="text-muted"> / </span>
                          <span className="text-loss">{stats.losses}</span>
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-muted">Partial Locked</p>
                        <p className="text-sm font-mono font-semibold text-warning">
                          ${stats.partial_pnl_locked.toFixed(2)}
                        </p>
                      </div>
                    </div>

                    {/* BTC HODL Benchmark */}
                    {stats.btc_hodl_pct !== undefined && (
                      <div className="pt-2 border-t border-border mt-1">
                        <p className="text-xs text-muted mb-1.5 font-semibold uppercase tracking-wider">vs BTC Hold</p>
                        <div className="flex items-center justify-between">
                          <div className="text-center flex-1">
                            <p className="text-xs text-muted">Bot P&L</p>
                            <p className={`text-sm font-mono font-bold ${stats.total_pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                              {stats.total_pnl >= 0 ? '+' : ''}${stats.total_pnl.toFixed(2)}
                            </p>
                          </div>
                          <div className="text-center px-2">
                            {stats.total_pnl >= stats.btc_hodl_pct * 100 ? (
                              <span className="text-lg">🏆</span>
                            ) : (
                              <span className="text-lg">📉</span>
                            )}
                          </div>
                          <div className="text-center flex-1">
                            <p className="text-xs text-muted">BTC HODL</p>
                            <p className={`text-sm font-mono font-bold ${stats.btc_hodl_pct >= 0 ? 'text-profit' : 'text-loss'}`}>
                              {stats.btc_hodl_pct >= 0 ? '+' : ''}{stats.btc_hodl_pct.toFixed(2)}%
                            </p>
                          </div>
                        </div>
                        <div className={`mt-2 text-center text-xs font-semibold py-1 rounded-lg ${
                          stats.total_pnl > 0 && stats.btc_hodl_pct <= 0
                            ? 'bg-profit/15 text-profit'
                            : stats.total_pnl > stats.btc_hodl_pct * 100
                            ? 'bg-profit/10 text-profit'
                            : 'bg-loss/10 text-loss'
                        }`}>
                          {stats.total_pnl > 0 && stats.btc_hodl_pct <= 0
                            ? '✓ Bot wins — HODL lost money'
                            : stats.total_pnl > 0
                            ? `Bot active vs HODL ${stats.btc_hodl_pct >= 0 ? '+' : ''}${stats.btc_hodl_pct.toFixed(2)}%`
                            : `HODL outperforming by ${(stats.btc_hodl_pct - (stats.total_pnl / 100)).toFixed(2)}%`
                          }
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Users Table */}
        <div className="bg-card border border-border rounded-2xl p-5">
          <div className="flex items-center justify-between mb-5">
            <h2 className="font-semibold text-sm flex items-center gap-2" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>
              All Users
              <span className="text-xs px-2 py-0.5 rounded-full bg-elevated text-muted">{users.length}</span>
            </h2>
            <div className="flex items-center gap-3">
              <input
                type="text"
                placeholder="Search by email..."
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="px-3 py-2 rounded-xl bg-elevated border border-border text-white text-sm placeholder-muted/40 focus:outline-none focus:border-accent w-64"
              />
              <button onClick={loadData} className="px-3 py-2 rounded-xl bg-elevated border border-border text-muted hover:text-white text-xs font-medium transition-colors">Refresh</button>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted border-b border-border">
                  <th className="pb-3 text-left font-medium">User</th>
                  <th className="pb-3 text-left font-medium">Signed Up</th>
                  <th className="pb-3 text-center font-medium">Status</th>
                  <th className="pb-3 text-center font-medium">Plan</th>
                  <th className="pb-3 text-right font-medium">Trades</th>
                  <th className="pb-3 text-right font-medium">Win Rate</th>
                  <th className="pb-3 text-right font-medium">Today P&L</th>
                  <th className="pb-3 text-right font-medium">7d P&L</th>
                  <th className="pb-3 text-right font-medium">30d P&L</th>
                  <th className="pb-3 text-right font-medium">All-Time P&L</th>
                  <th className="pb-3 text-right font-medium">Balance</th>
                  <th className="pb-3 text-center font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredUsers.map(u => (
                  <tr key={u.id} className="border-b border-border/30 hover:bg-elevated/30 transition-colors">
                    {/* User */}
                    <td className="py-3.5">
                      <div>
                        <p className="font-medium text-sm">{u.email}</p>
                        <p className="text-xs text-muted font-mono mt-0.5">{u.id.slice(0, 8)}...</p>
                      </div>
                    </td>
                    {/* Signed Up */}
                    <td className="py-3.5">
                      <p className="text-xs">{formatDate(u.signed_up)}</p>
                      <p className="text-xs text-muted">{formatTime(u.signed_up)}</p>
                    </td>
                    {/* Status */}
                    <td className="py-3.5 text-center">
                      <div className="flex flex-col items-center gap-1">
                        {u.bot_active ? (
                          <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-profit/15 text-profit border border-profit/30 font-semibold">
                            <span className="w-1.5 h-1.5 rounded-full bg-profit animate-pulse"></span>
                            Running
                          </span>
                        ) : (
                          <span className="text-xs px-2 py-0.5 rounded-full bg-border text-muted font-medium">Inactive</span>
                        )}
                        {u.has_api_keys && (
                          <span className="text-xs text-profit/70">API keys</span>
                        )}
                        {u.open_trades > 0 && (
                          <span className="text-xs text-warning">{u.open_trades} open</span>
                        )}
                      </div>
                    </td>
                    {/* Plan */}
                    <td className="py-3.5 text-center">
                      {u.is_premium ? (
                        <div>
                          <span className="text-xs px-2.5 py-1 rounded-full bg-purple-500/20 text-purple-400 border border-purple-500/30 font-bold">PRO</span>
                          {u.calibration_count > 0 && (
                            <p className="text-xs text-muted mt-1">{u.calibration_count} calibrations</p>
                          )}
                        </div>
                      ) : (
                        <span className="text-xs px-2 py-0.5 rounded-full bg-border text-muted font-medium">Free</span>
                      )}
                    </td>
                    {/* Trades */}
                    <td className="py-3.5 text-right">
                      <p className="font-mono font-semibold">{u.total_trades}</p>
                      <p className="text-xs text-muted">{u.wins}W / {u.losses}L</p>
                    </td>
                    {/* Win Rate */}
                    <td className="py-3.5 text-right">
                      <p className={`font-mono font-semibold ${u.win_rate >= 50 ? 'text-profit' : u.win_rate > 0 ? 'text-loss' : 'text-muted'}`}>
                        {u.total_trades > 0 ? `${u.win_rate}%` : '\u2014'}
                      </p>
                    </td>
                    {/* Today P&L */}
                    <td className="py-3.5 text-right">
                      <p className={`font-mono font-semibold text-sm ${u.daily_pnl > 0 ? 'text-profit' : u.daily_pnl < 0 ? 'text-loss' : 'text-muted'}`}>
                        {u.daily_pnl !== 0 ? `${u.daily_pnl >= 0 ? '+' : ''}$${u.daily_pnl.toFixed(2)}` : '—'}
                      </p>
                    </td>
                    {/* 7d P&L */}
                    <td className="py-3.5 text-right">
                      <p className={`font-mono font-semibold text-sm ${u.weekly_pnl > 0 ? 'text-profit' : u.weekly_pnl < 0 ? 'text-loss' : 'text-muted'}`}>
                        {u.weekly_pnl !== 0 ? `${u.weekly_pnl >= 0 ? '+' : ''}$${u.weekly_pnl.toFixed(2)}` : '—'}
                      </p>
                    </td>
                    {/* 30d P&L */}
                    <td className="py-3.5 text-right">
                      <p className={`font-mono font-semibold text-sm ${u.monthly_pnl > 0 ? 'text-profit' : u.monthly_pnl < 0 ? 'text-loss' : 'text-muted'}`}>
                        {u.monthly_pnl !== 0 ? `${u.monthly_pnl >= 0 ? '+' : ''}$${u.monthly_pnl.toFixed(2)}` : '—'}
                      </p>
                    </td>
                    {/* All-Time P&L */}
                    <td className="py-3.5 text-right">
                      <p className={`font-mono font-semibold ${u.total_pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                        {u.total_trades > 0 ? `${u.total_pnl >= 0 ? '+' : ''}$${u.total_pnl.toFixed(2)}` : '\u2014'}
                      </p>
                    </td>
                    {/* Balance */}
                    <td className="py-3.5 text-right">
                      <p className="font-mono text-warning font-semibold">${u.demo_balance.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</p>
                      <p className="text-xs text-muted">{u.has_api_keys ? 'Live' : 'Demo'} equity</p>
                      {u.open_position_value > 0 && (
                        <p className="text-xs text-accent/70 font-mono mt-0.5">
                          ${u.cash_balance.toFixed(0)} cash + ${u.open_position_value.toFixed(0)} in trade
                        </p>
                      )}
                    </td>
                    {/* Actions */}
                    <td className="py-3.5 text-center">
                      <button
                        onClick={() => togglePremium(u.id)}
                        disabled={toggleLoading === u.id}
                        className={`text-xs px-3 py-1.5 rounded-lg font-semibold transition-colors disabled:opacity-50 ${
                          u.is_premium
                            ? 'bg-loss/15 border border-loss/30 text-loss hover:bg-loss/25'
                            : 'bg-purple-500/15 border border-purple-500/30 text-purple-400 hover:bg-purple-500/25'
                        }`}
                      >
                        {toggleLoading === u.id ? '...' : u.is_premium ? 'Remove Pro' : 'Give Pro'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {filteredUsers.length === 0 && (
            <div className="text-center py-10 text-muted text-sm">
              {search ? 'No users match your search.' : 'No users yet.'}
            </div>
          )}
        </div>

        {/* Quick Stats */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
          {/* Top Performers */}
          <div className="bg-card border border-border rounded-2xl p-5">
            <h3 className="text-xs font-semibold text-muted uppercase tracking-wider mb-4">Top Performers</h3>
            <div className="space-y-2">
              {[...users]
                .filter(u => u.total_trades > 0)
                .sort((a, b) => b.total_pnl - a.total_pnl)
                .slice(0, 5)
                .map((u, i) => (
                  <div key={u.id} className="flex items-center justify-between px-3 py-2 rounded-xl bg-elevated">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-bold text-muted w-4">#{i + 1}</span>
                      <span className="text-xs truncate max-w-32">{u.email}</span>
                    </div>
                    <span className={`text-xs font-mono font-bold ${u.total_pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                      {u.total_pnl >= 0 ? '+' : ''}${u.total_pnl.toFixed(2)}
                    </span>
                  </div>
                ))}
              {users.filter(u => u.total_trades > 0).length === 0 && (
                <p className="text-muted text-xs text-center py-4">No trading activity yet.</p>
              )}
            </div>
          </div>

          {/* Most Active */}
          <div className="bg-card border border-border rounded-2xl p-5">
            <h3 className="text-xs font-semibold text-muted uppercase tracking-wider mb-4">Most Active Traders</h3>
            <div className="space-y-2">
              {[...users]
                .sort((a, b) => b.total_trades - a.total_trades)
                .slice(0, 5)
                .filter(u => u.total_trades > 0)
                .map((u, i) => (
                  <div key={u.id} className="flex items-center justify-between px-3 py-2 rounded-xl bg-elevated">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-bold text-muted w-4">#{i + 1}</span>
                      <span className="text-xs truncate max-w-32">{u.email}</span>
                    </div>
                    <span className="text-xs font-mono font-bold text-accent">{u.total_trades} trades</span>
                  </div>
                ))}
            </div>
          </div>

          {/* Pro Users */}
          <div className="bg-card border border-purple-500/20 rounded-2xl p-5">
            <h3 className="text-xs font-semibold text-purple-400 uppercase tracking-wider mb-4">Pro Subscribers</h3>
            <div className="space-y-2">
              {users.filter(u => u.is_premium).length > 0 ? (
                users.filter(u => u.is_premium).map(u => (
                  <div key={u.id} className="flex items-center justify-between px-3 py-2 rounded-xl bg-elevated">
                    <div className="flex items-center gap-2">
                      <span className="text-purple-400">&#x1f451;</span>
                      <span className="text-xs truncate max-w-32">{u.email}</span>
                    </div>
                    <div className="text-right">
                      <span className="text-xs font-mono text-purple-400">{u.calibration_count} cal.</span>
                      {u.premium_since && <p className="text-xs text-muted">{formatDate(u.premium_since)}</p>}
                    </div>
                  </div>
                ))
              ) : (
                <p className="text-muted text-xs text-center py-4">No pro subscribers yet.</p>
              )}
              <div className="mt-3 pt-3 border-t border-border">
                <div className="flex justify-between text-xs">
                  <span className="text-muted">Monthly revenue</span>
                  <span className="font-bold text-profit font-mono">${summary?.monthly_premium_revenue ?? 0}/mo</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  )
}
