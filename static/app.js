// ═══════════════════════════════════════════════════════════
// Live Demo Real-time Chart Manager
// ═══════════════════════════════════════════════════════════
const LiveChart = {
    chart: null,
    labels: [],
    successData: [],
    totalTxn: 0,
    successCount: 0,
    failCount: 0,

    init() {
        const ctx = document.getElementById('live-chart').getContext('2d');
        const gradient = ctx.createLinearGradient(0, 0, 0, 280);
        gradient.addColorStop(0, 'rgba(74, 222, 128, 0.25)');
        gradient.addColorStop(1, 'rgba(74, 222, 128, 0.0)');

        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: this.labels,
                datasets: [{
                    label: 'Tỷ lệ thành công (%)',
                    data: this.successData,
                    borderColor: '#4ade80',
                    backgroundColor: gradient,
                    borderWidth: 2.5,
                    fill: true,
                    tension: 0.35,
                    pointRadius: 0,
                    pointHitRadius: 10
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 0 },
                scales: {
                    x: {
                        title: { display: true, text: 'Giao dịch #', color: '#7b8ab0', font: { size: 11 } },
                        ticks: { color: '#4a5578', maxTicksLimit: 15, font: { size: 10 } },
                        grid: { color: 'rgba(255,255,255,0.03)' }
                    },
                    y: {
                        min: 0, max: 105,
                        title: { display: true, text: 'Tỷ lệ thành công (%)', color: '#7b8ab0', font: { size: 11 } },
                        ticks: { color: '#4a5578', callback: v => v + '%', font: { size: 10 } },
                        grid: { color: 'rgba(255,255,255,0.03)' }
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(15,23,50,0.9)',
                        borderColor: 'rgba(99,128,255,0.2)',
                        borderWidth: 1,
                        titleFont: { family: 'JetBrains Mono' },
                        bodyFont: { family: 'JetBrains Mono' },
                        callbacks: { label: ctx => `Tỷ lệ: ${ctx.parsed.y.toFixed(1)}%` }
                    }
                }
            }
        });
    },

    addTransaction(success) {
        this.totalTxn++;
        if (success) this.successCount++;
        else this.failCount++;

        const rate = (this.successCount / this.totalTxn) * 100;
        this.labels.push(this.totalTxn);
        this.successData.push(parseFloat(rate.toFixed(1)));

        if (this.labels.length > 200) {
            this.labels.shift();
            this.successData.shift();
        }

        this.chart.update();
        this.updateStats();
    },

    addEvent(type) {
        if (!this.chart) return;
        const ctx = this.chart.ctx;
        const gradient = ctx.createLinearGradient(0, 0, 0, 280);
        if (type === 'kill') {
            this.chart.data.datasets[0].borderColor = '#f87171';
            gradient.addColorStop(0, 'rgba(248, 113, 113, 0.25)');
            gradient.addColorStop(1, 'rgba(248, 113, 113, 0.0)');
        } else {
            this.chart.data.datasets[0].borderColor = '#4ade80';
            gradient.addColorStop(0, 'rgba(74, 222, 128, 0.25)');
            gradient.addColorStop(1, 'rgba(74, 222, 128, 0.0)');
        }
        this.chart.data.datasets[0].backgroundColor = gradient;
        this.chart.update();
    },

    updateStats() {
        document.getElementById('stat-total').textContent = this.totalTxn;
        document.getElementById('stat-success').textContent = this.successCount;
        document.getElementById('stat-fail').textContent = this.failCount;
        const rate = this.totalTxn > 0 ? ((this.successCount / this.totalTxn) * 100).toFixed(1) : '—';
        document.getElementById('stat-rate').textContent = rate + (this.totalTxn > 0 ? '%' : '');
    },

    reset() {
        this.labels = [];
        this.successData = [];
        this.totalTxn = 0;
        this.successCount = 0;
        this.failCount = 0;
        if (this.chart) {
            this.chart.data.labels = this.labels;
            this.chart.data.datasets[0].data = this.successData;
            this.chart.data.datasets[0].borderColor = '#4ade80';
            this.chart.update();
        }
        this.updateStats();
    },

    destroy() {
        if (this.chart) { this.chart.destroy(); this.chart = null; }
    }
};


// ═══════════════════════════════════════════════════════════
// Main Application
// ═══════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
    const btnStart = document.getElementById('btn-start');
    const inputThreads = document.getElementById('input-threads');
    const inputRuns = document.getElementById('input-runs');
    const terminalOutput = document.getElementById('terminal-output');
    const runStatus = document.getElementById('run-status');
    let eventSource = null;

    // ── Helpers ───────────────────────────────────────────
    function appendLog(text) {
        const div = document.createElement('div');
        div.className = 'terminal-line';

        if (text.includes('THẤT BẠI') || text.includes('ERROR') || text.includes('SẬP')) {
            div.classList.add('error');
        } else if (text.includes('BẮN HẠ') || text.includes('KILLED') || text.includes('WARNING')) {
            div.classList.add('warning');
        } else if (text.includes('THÀNH CÔNG') || text.includes('ONLINE') || text.includes('PHỤC HỒI')) {
            div.classList.add('success');
        } else if (text.includes('THỐNG KÊ')) {
            div.classList.add('stats');
        } else if (text.includes('INFO') || text.includes('succeeded')) {
            div.classList.add('info');
        }

        div.textContent = text;
        terminalOutput.appendChild(div);
        terminalOutput.scrollTop = terminalOutput.scrollHeight;
    }

    function closeSSE() {
        if (eventSource) { eventSource.close(); eventSource = null; }
    }

    function showBenchmarkCharts() {
        document.getElementById('section-benchmark-charts').style.display = 'block';
        document.getElementById('section-demo-chart').style.display = 'none';
    }

    function showDemoChart() {
        document.getElementById('section-benchmark-charts').style.display = 'none';
        document.getElementById('section-demo-chart').style.display = 'block';
    }

    function setStatus(text, mode) {
        runStatus.textContent = text;
        runStatus.className = 'status-badge';
        if (mode === 'running') runStatus.classList.add('running');
        else if (mode === 'demo') runStatus.classList.add('demo-mode');
    }

    // ── BENCHMARK MODE ────────────────────────────────────
    btnStart.addEventListener('click', () => {
        const threads = inputThreads.value;
        const runs = inputRuns.value;

        terminalOutput.innerHTML = '';
        appendLog(`> Starting benchmark with ${threads} threads and ${runs} runs...`);
        btnStart.disabled = true;
        btnStart.textContent = '⏳ ĐANG CHẠY...';
        setStatus('RUNNING', 'running');
        showBenchmarkCharts();

        closeSSE();
        eventSource = new EventSource(`/api/run_benchmark?threads=${threads}&runs=${runs}`);

        eventSource.onmessage = function(event) {
            if (event.data === '[DONE]') {
                closeSSE();
                appendLog('> Benchmark completed.');
                btnStart.disabled = false;
                btnStart.textContent = '▶ START BENCHMARK';
                setStatus('COMPLETED', '');
                refreshCharts();
            } else {
                appendLog(event.data);
            }
        };

        eventSource.onerror = function() {
            closeSSE();
            appendLog('> Connection lost.');
            btnStart.disabled = false;
            btnStart.textContent = '▶ START BENCHMARK';
            setStatus('ERROR', '');
        };
    });

    // ── FAULT DEMO MODE ───────────────────────────────────
    window.startFaultDemo = function() {
        const btnDemoStart = document.getElementById('btn-demo-start');
        const btnDemoStop = document.getElementById('btn-demo-stop');

        terminalOutput.innerHTML = '';
        appendLog('> Starting Fault Tolerance Live Demo...');
        btnDemoStart.disabled = true;
        btnDemoStop.disabled = false;
        btnStart.disabled = true;
        setStatus('LIVE DEMO', 'demo');

        showDemoChart();
        LiveChart.destroy();
        LiveChart.reset();
        LiveChart.init();

        closeSSE();
        eventSource = new EventSource('/api/fault_demo');

        eventSource.onmessage = function(event) {
            if (event.data === '[DONE]') {
                closeSSE();
                appendLog('> Live Demo ended.');
                btnDemoStart.disabled = false;
                btnDemoStop.disabled = true;
                btnStart.disabled = false;
                setStatus('READY', '');
            } else {
                appendLog(event.data);
                if (event.data.includes('THÀNH CÔNG')) LiveChart.addTransaction(true);
                else if (event.data.includes('THẤT BẠI')) LiveChart.addTransaction(false);
            }
        };

        eventSource.onerror = function() {
            closeSSE();
            appendLog('> Demo connection lost.');
            btnDemoStart.disabled = false;
            btnDemoStop.disabled = true;
            btnStart.disabled = false;
            setStatus('READY', '');
        };
    };

    window.stopFaultDemo = function() {
        fetch('/api/stop_demo', { method: 'POST' }).then(() => {
            closeSSE();
            appendLog('> Demo stopped by user.');
            document.getElementById('btn-demo-start').disabled = false;
            document.getElementById('btn-demo-stop').disabled = true;
            btnStart.disabled = false;
            setStatus('READY', '');
        });
    };
});


// ═══════════════════════════════════════════════════════════
// Kill / Revive
// ═══════════════════════════════════════════════════════════
async function killSite(siteId) {
    try {
        const res = await fetch(`/api/kill/${siteId}`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            updateSiteStatus(siteId, false);
            logAdmin(`ADMIN: Site ${siteId} đã bị BẮN HẠ (KILLED)!`, 'warning');
            LiveChart.addEvent('kill');
        } else { alert('Failed: ' + data.message); }
    } catch (err) { alert('Error: ' + err); }
}

async function reviveSite(siteId) {
    try {
        const res = await fetch(`/api/revive/${siteId}`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            updateSiteStatus(siteId, true);
            logAdmin(`ADMIN: Site ${siteId} đã được PHỤC HỒI (REVIVED)!`, 'success');
            LiveChart.addEvent('revive');
        } else { alert('Failed: ' + data.message); }
    } catch (err) { alert('Error: ' + err); }
}

function logAdmin(text, cls) {
    const out = document.getElementById('terminal-output');
    const div = document.createElement('div');
    div.className = `terminal-line ${cls}`;
    div.textContent = text;
    out.appendChild(div);
    out.scrollTop = out.scrollHeight;
}

function updateSiteStatus(siteId, isAlive) {
    const dot = document.getElementById(`indicator-${siteId}`);
    if (dot) {
        dot.className = isAlive ? 'status-dot alive' : 'status-dot dead';
    }
}


// ═══════════════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════════════
function copyTerminal() {
    const out = document.getElementById('terminal-output');
    const text = out.innerText;
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.getElementById('btn-copy');
        const label = document.getElementById('copy-label');
        btn.classList.add('copied');
        label.textContent = 'Copied!';
        setTimeout(() => {
            btn.classList.remove('copied');
            label.textContent = 'Copy';
        }, 2000);
    });
}

function clearTerminal() {
    const out = document.getElementById('terminal-output');
    out.innerHTML = '<div class="terminal-line info">Terminal cleared.</div>';
}

function refreshCharts() {
    const t = Date.now();
    ['chart-abort', 'chart-throughput', 'chart-latency'].forEach(id => {
        const img = document.getElementById(id);
        if (img) {
            img.src = img.src.split('?')[0] + '?t=' + t;
            img.style.display = 'block';
        }
    });
}
