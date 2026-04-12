import { useState, useEffect, useRef } from 'react'
import { useNavigate, Link } from 'react-router-dom'

const FAKE_TRADES = [
  { symbol: 'BTC-USD', side: 'BUY', entry: 67420.50, current: 68104.20, pnl: +683.70, pnlPct: +1.01 },
  { symbol: 'ETH-USD', side: 'SELL', entry: 3521.00, current: 3488.40, pnl: +32.60, pnlPct: +0.93 },
  { symbol: 'SOL-USD', side: 'BUY', entry: 142.30, current: 147.80, pnl: +5.50, pnlPct: +3.87 },
]

function MockDashboard() {
  const [prices, setPrices] = useState(FAKE_TRADES.map(t => t.current))
  const [tick, setTick] = useState(0)

  useEffect(() => {
    const interval = setInterval(() => {
      setPrices(prev => prev.map((p, i) => {
        const delta = (Math.random() - 0.48) * FAKE_TRADES[i].entry * 0.001
        return Math.max(p + delta, FAKE_TRADES[i].entry * 0.9)
      }))
      setTick(t => t + 1)
    }, 1800)
    return () => clearInterval(interval)
  }, [])

  return (
    <div className="float relative rounded-2xl overflow-hidden border border-border bg-card shadow-2xl" style={{ boxShadow: '0 0 60px rgba(59,130,246,0.15)' }}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-elevated">
        <div className="flex items-center gap-2">
          <div className="w-3 h-3 rounded-full bg-loss"></div>
          <div className="w-3 h-3 rounded-full bg-warning"></div>
          <div className="w-3 h-3 rounded-full bg-profit"></div>
        </div>
        <span className="text-xs text-muted font-mono">cryptobot — live dashboard</span>
        <div className="flex items-center gap-1.5">
          <div className="pulse-dot w-2 h-2 rounded-full bg-profit"></div>
          <span className="text-xs text-profit">LIVE</span>
        </div>
      </div>

      {/* Bot Status */}
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="pulse-dot w-2.5 h-2.5 rounded-full bg-profit"></div>
          <span className="text-sm font-medium text-white">Bot Running · BTC-USD</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs px-2 py-0.5 rounded-full bg-profit/20 text-profit font-mono">Z={((tick % 40) * 0.05 + 1.8).toFixed(2)}</span>
          <span className="text-xs text-muted font-mono">${prices[0].toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
        </div>
      </div>

      {/* Open Trades */}
      <div className="px-4 py-2 border-b border-border">
        <p className="text-xs text-muted mb-2 uppercase tracking-wider">Open Positions</p>
        {FAKE_TRADES.map((trade, i) => {
          const livePrice = prices[i]
          const livePnl = trade.side === 'BUY' ? livePrice - trade.entry : trade.entry - livePrice
          const livePct = (livePnl / trade.entry) * 100
          const isProfit = livePnl > 0
          return (
            <div key={i} className="flex items-center justify-between py-1.5">
              <div className="flex items-center gap-2">
                <span className={`text-xs px-1.5 py-0.5 rounded font-bold ${trade.side === 'BUY' ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'}`}>
                  {trade.side}
                </span>
                <span className="text-sm font-mono text-white">{trade.symbol}</span>
              </div>
              <div className="text-right">
                <span className={`text-sm font-mono font-medium ${isProfit ? 'text-profit' : 'text-loss'}`}>
                  {isProfit ? '+' : ''}{livePct.toFixed(2)}%
                </span>
              </div>
            </div>
          )
        })}
      </div>

      {/* AI Grade */}
      <div className="px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted">Last AI Analysis</span>
          <span className="text-xs px-2 py-0.5 rounded bg-accent/20 text-accent font-bold">Grade A</span>
        </div>
        <span className="text-xs text-muted">Win Rate: <span className="text-profit font-mono">73.2%</span></span>
      </div>
    </div>
  )
}

export function Landing() {
  const navigate = useNavigate()
  const [scrolled, setScrolled] = useState(false)

  useEffect(() => {
    const handler = () => setScrolled(window.scrollY > 20)
    window.addEventListener('scroll', handler)
    return () => window.removeEventListener('scroll', handler)
  }, [])

  return (
    <div className="min-h-screen bg-dark text-white" style={{ fontFamily: 'Inter, sans-serif' }}>
      {/* Navbar */}
      <nav className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 ${scrolled ? 'bg-dark/90 backdrop-blur-md border-b border-border' : ''}`}>
        <div className="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-2xl">⚡</span>
            <span className="text-lg font-bold" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>CryptoBot</span>
          </div>
          <div className="flex items-center gap-4">
            <Link to="/login" className="text-sm text-muted hover:text-white transition-colors">Login</Link>
            <button
              onClick={() => navigate('/signup')}
              className="text-sm px-4 py-2 rounded-lg bg-accent hover:bg-blue-500 transition-colors font-medium"
            >
              Sign Up
            </button>
          </div>
        </div>
      </nav>

      {/* Hero */}
      <section className="relative min-h-screen flex items-center pt-16 overflow-hidden">
        {/* Background glow */}
        <div className="absolute inset-0 pointer-events-none">
          <div className="absolute top-1/3 left-1/4 w-96 h-96 rounded-full opacity-10" style={{ background: 'radial-gradient(circle, #3B82F6 0%, transparent 70%)' }}></div>
          <div className="absolute top-1/2 right-1/4 w-64 h-64 rounded-full opacity-8" style={{ background: 'radial-gradient(circle, #8B5CF6 0%, transparent 70%)' }}></div>
        </div>

        <div className="relative max-w-7xl mx-auto px-6 py-20 grid grid-cols-1 lg:grid-cols-2 gap-16 items-center">
          {/* Left */}
          <div>
            <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full border border-border bg-elevated text-xs text-muted mb-8">
              <div className="w-1.5 h-1.5 rounded-full bg-profit pulse-dot"></div>
              Powered by Claude AI · Robinhood Crypto
            </div>
            <h1 className="text-5xl lg:text-6xl font-extrabold leading-tight mb-6" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>
              Your Crypto Bot.{' '}
              <span className="gradient-text">Always On.</span>{' '}
              Always Learning.
            </h1>
            <p className="text-lg text-muted leading-relaxed mb-8 max-w-lg">
              Automate your Robinhood crypto trades with a Z-Score strategy, then let AI analyze every trade and improve itself daily.
            </p>
            <div className="flex flex-col sm:flex-row gap-4 mb-6">
              <button
                onClick={() => navigate('/signup')}
                className="glow px-8 py-4 rounded-xl bg-accent hover:bg-blue-500 transition-all font-semibold text-base"
                style={{ boxShadow: '0 0 30px rgba(59,130,246,0.4)' }}
              >
                Start Free →
              </button>
              <a
                href="#how-it-works"
                className="px-8 py-4 rounded-xl border border-border hover:border-accent/50 transition-colors font-semibold text-base text-center"
              >
                See How It Works
              </a>
            </div>
            <p className="text-xs text-muted">No subscription required · Powered by Claude AI · Robinhood Crypto</p>
          </div>

          {/* Right - Mock Dashboard */}
          <div className="hidden lg:block">
            <MockDashboard />
          </div>
        </div>
      </section>

      {/* How It Works */}
      <section id="how-it-works" className="py-24 bg-card border-t border-border">
        <div className="max-w-7xl mx-auto px-6">
          <div className="text-center mb-16">
            <h2 className="text-4xl font-bold mb-4" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>How It Works</h2>
            <p className="text-muted max-w-lg mx-auto">Three simple steps to automated crypto trading with AI-powered analysis.</p>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
            {[
              {
                num: '01',
                icon: '🔑',
                title: 'Connect Robinhood',
                desc: 'Add your Robinhood Crypto API key in 30 seconds. Your keys are stored securely and never shared.'
              },
              {
                num: '02',
                icon: '🤖',
                title: 'Bot Runs 24/7',
                desc: 'The Z-Score strategy trades automatically while you sleep. No manual intervention needed.'
              },
              {
                num: '03',
                icon: '🧠',
                title: 'AI Reviews Every Trade',
                desc: 'Claude analyzes each closed trade and sends you a daily performance report with actionable insights.'
              }
            ].map((step, i) => (
              <div key={i} className="relative p-8 rounded-2xl border border-border bg-elevated hover:border-accent/30 transition-colors">
                <div className="text-6xl font-black text-border/50 mb-4" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>{step.num}</div>
                <div className="text-3xl mb-4">{step.icon}</div>
                <h3 className="text-xl font-bold mb-2">{step.title}</h3>
                <p className="text-muted text-sm leading-relaxed">{step.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Features */}
      <section className="py-24 bg-dark border-t border-border">
        <div className="max-w-7xl mx-auto px-6">
          <div className="text-center mb-16">
            <h2 className="text-4xl font-bold mb-4" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>Everything You Need</h2>
            <p className="text-muted max-w-lg mx-auto">A complete automated trading platform built for serious crypto traders.</p>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {[
              { icon: '📊', title: 'Live Dashboard', desc: 'Real-time P&L, open positions, and live price feed updated every minute.' },
              { icon: '🤖', title: 'AI Trade Analysis', desc: 'Grade, entry quality, exit quality, and what to improve — after every trade.' },
              { icon: '📝', title: 'Daily Reports', desc: 'Win rate, avg R:R, and the top suggestion from Claude delivered every morning.' },
              { icon: '🛡️', title: 'Risk Management', desc: 'Stop loss, take profit, and trailing stop built into every trade automatically.' },
              { icon: '📈', title: 'Z-Score Strategy', desc: 'Battle-tested mean reversion signal identifies high-probability entry points.' },
              { icon: '🔒', title: 'Your Data, Your Keys', desc: 'API keys stored securely on your account. Never shared, never exposed.' },
            ].map((feature, i) => (
              <div key={i} className="p-6 rounded-2xl border border-border bg-card hover:border-accent/30 hover:bg-elevated transition-all">
                <div className="text-2xl mb-3">{feature.icon}</div>
                <h3 className="font-bold mb-2">{feature.title}</h3>
                <p className="text-muted text-sm leading-relaxed">{feature.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Security Bar */}
      <section className="py-6 bg-elevated border-t border-b border-border">
        <div className="max-w-7xl mx-auto px-6">
          <div className="flex flex-wrap items-center justify-center gap-6 text-sm text-muted">
            <span>🔒 Your API key never leaves your account</span>
            <span className="hidden sm:block text-border">·</span>
            <span>Read-only market data</span>
            <span className="hidden sm:block text-border">·</span>
            <span>Trade-only permissions</span>
            <span className="hidden sm:block text-border">·</span>
            <span>No withdrawal access</span>
          </div>
        </div>
      </section>

      {/* Final CTA */}
      <section className="py-24 bg-dark">
        <div className="max-w-2xl mx-auto px-6 text-center">
          <h2 className="text-4xl font-bold mb-4" style={{ fontFamily: 'Space Grotesk, sans-serif' }}>
            Ready to automate your crypto trading?
          </h2>
          <p className="text-muted mb-8">Join traders using CryptoBot to automate their Robinhood crypto strategy.</p>
          <button
            onClick={() => navigate('/signup')}
            className="glow px-10 py-4 rounded-xl bg-accent hover:bg-blue-500 transition-all font-semibold text-lg"
            style={{ boxShadow: '0 0 40px rgba(59,130,246,0.4)' }}
          >
            Create Free Account →
          </button>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-border py-8">
        <div className="max-w-7xl mx-auto px-6 text-center text-muted text-sm">
          <div className="flex items-center justify-center gap-2 mb-2">
            <span>⚡</span>
            <span className="font-bold text-white">CryptoBot</span>
          </div>
          <p>Automated crypto trading powered by Claude AI · Not financial advice</p>
        </div>
      </footer>
    </div>
  )
}
