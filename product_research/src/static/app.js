/**
 * Autonomous AI Sales Product Intelligence - Client Application
 */

let currentProvider = 'ollama';
let currentResultTab = 'overview';
let latestGeneratedData = null;

// Initialize on DOM load
document.addEventListener('DOMContentLoaded', () => {
  if (window.lucide) {
    lucide.createIcons();
  }
  refreshOllamaStatus();
});

// ---------------------------------------------------------
// Ollama Status & Model Scanner
// ---------------------------------------------------------

async function refreshOllamaStatus() {
  const badge = document.getElementById('ollama-status-badge');
  const dot = document.getElementById('ollama-dot');
  const text = document.getElementById('ollama-status-text');
  const ollamaUrl = document.getElementById('ollama-url').value || 'http://localhost:11434';

  text.innerText = 'Checking Ollama...';
  dot.className = 'w-2 h-2 rounded-full bg-amber-400 animate-pulse';

  try {
    const res = await fetch(`/api/ollama/status?ollama_url=${encodeURIComponent(ollamaUrl)}`);
    const data = await res.json();

    if (data.online) {
      dot.className = 'w-2 h-2 rounded-full bg-emerald-400';
      text.innerText = `Ollama Online (${data.models.length} models)`;
      text.className = 'text-emerald-300 font-medium';
      
      // Update model dropdown if models found
      if (data.models && data.models.length > 0) {
        const select = document.getElementById('ollama-model-select');
        select.innerHTML = '';
        data.models.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m;
          opt.innerText = m;
          select.appendChild(opt);
        });
        // Add manual entry option
        const customOpt = document.createElement('option');
        customOpt.value = 'custom';
        customOpt.innerText = '✍️ Enter Custom Tag Manually...';
        select.appendChild(customOpt);
      }
    } else {
      dot.className = 'w-2 h-2 rounded-full bg-slate-500';
      text.innerText = 'Ollama Offline (Using Fallback)';
      text.className = 'text-slate-400';
    }
  } catch (err) {
    dot.className = 'w-2 h-2 rounded-full bg-rose-500';
    text.innerText = 'Ollama Offline';
    text.className = 'text-rose-400';
  }
}

async function fetchOllamaModels() {
  await refreshOllamaStatus();
}

function onOllamaSelectChange(val) {
  const manualInput = document.getElementById('ollama-manual-model');
  if (val === 'custom') {
    manualInput.classList.remove('hidden');
    manualInput.focus();
  } else {
    manualInput.classList.add('hidden');
  }
}

// ---------------------------------------------------------
// Provider Switching
// ---------------------------------------------------------

function switchProvider(provider) {
  currentProvider = provider;
  
  // Update Tab buttons
  document.querySelectorAll('.provider-tab').forEach(tab => {
    tab.classList.remove('active', 'bg-indigo-600', 'text-white');
    tab.classList.add('text-slate-400');
  });
  const activeTab = document.getElementById(`tab-${provider}`);
  if (activeTab) {
    activeTab.classList.add('active', 'bg-indigo-600', 'text-white');
    activeTab.classList.remove('text-slate-400');
  }

  // Show corresponding config panel
  document.querySelectorAll('.provider-config').forEach(c => c.classList.add('hidden'));
  const activeConfig = document.getElementById(`config-${provider}`);
  if (activeConfig) {
    activeConfig.classList.remove('hidden');
  }
}

// ---------------------------------------------------------
// Presets
// ---------------------------------------------------------

function setPreset(url, name) {
  document.getElementById('target-url').value = url;
  document.getElementById('product-name').value = name;
}

// ---------------------------------------------------------
// Pipeline Execution (Form Submit)
// ---------------------------------------------------------

async function handleGenerate(event) {
  event.preventDefault();

  const url = document.getElementById('target-url').value.trim();
  const name = document.getElementById('product-name').value.trim() || null;
  const maxPages = parseInt(document.getElementById('max-pages-slider').value) || 15;
  const generateBtn = document.getElementById('generate-btn');

  // Determine model and api key based on provider
  let model = null;
  let apiKey = null;
  let customBaseUrl = null;
  let ollamaUrl = document.getElementById('ollama-url').value.trim() || 'http://localhost:11434';

  if (currentProvider === 'ollama') {
    const sel = document.getElementById('ollama-model-select').value;
    if (sel === 'custom') {
      model = document.getElementById('ollama-manual-model').value.trim() || 'llama3.3';
    } else {
      model = sel;
    }
  } else if (currentProvider === 'kimi') {
    apiKey = document.getElementById('kimi-key').value.trim();
    customBaseUrl = document.getElementById('kimi-base-url').value.trim() || 'https://api.moonshot.cn/v1';
    model = document.getElementById('kimi-model').value.trim() || 'moonshot-v1-32k';
  } else if (currentProvider === 'openai') {
    apiKey = document.getElementById('openai-key').value.trim();
    model = document.getElementById('openai-model').value;
  } else if (currentProvider === 'gemini') {
    apiKey = document.getElementById('gemini-key').value.trim();
    model = document.getElementById('gemini-model').value;
  } else if (currentProvider === 'anthropic') {
    apiKey = document.getElementById('anthropic-key').value.trim();
    model = document.getElementById('anthropic-model').value;
  }

  // Show progress box
  const progressCard = document.getElementById('progress-card');
  const progressTitle = document.getElementById('progress-title');
  const progressSubtitle = document.getElementById('progress-subtitle');
  const progressPct = document.getElementById('progress-pct');
  const progressBar = document.getElementById('progress-bar');
  const terminalLogs = document.getElementById('terminal-logs');

  progressCard.classList.remove('hidden');
  generateBtn.disabled = true;
  generateBtn.classList.add('opacity-50', 'cursor-not-allowed');

  // Initial Progress Stage: Crawling
  progressTitle.innerText = `Crawling ${url}...`;
  progressSubtitle.innerText = `Discovering sitemaps & parsing priority pages (up to ${maxPages} pages)...`;
  progressPct.innerText = '25%';
  progressBar.style.width = '25%';
  terminalLogs.innerHTML = `<div>[INFO] Target: ${url}</div><div>[INFO] Starting async multi-page crawler...</div>`;

  try {
    const payload = {
      url: url,
      name: name,
      provider: currentProvider,
      model: model,
      api_key: apiKey,
      ollama_url: ollamaUrl,
      custom_base_url: customBaseUrl,
      max_pages: maxPages,
      concurrency: 5
    };


    // Simulate progress updates while backend executes
    const logInterval = setInterval(() => {
      const currentWidth = parseInt(progressBar.style.width);
      if (currentWidth < 85) {
        const next = currentWidth + 15;
        progressBar.style.width = `${next}%`;
        progressPct.innerText = `${next}%`;
        if (next === 40) {
          progressTitle.innerText = 'Extracting 7-Pillar Product Knowledge...';
          progressSubtitle.innerText = `Feeding clean markdown into ${currentProvider.toUpperCase()} (${model || 'default'})...`;
          terminalLogs.innerHTML += `<div>[INFO] Clean markdown compiled. Launching structured LLM extractor...</div>`;
        } else if (next === 70) {
          progressTitle.innerText = 'Synthesizing GTM Sales Playbooks...';
          progressSubtitle.innerText = 'Incorporating zarif3624 discovery & louisblythe objection loops...';
          terminalLogs.innerHTML += `<div>[INFO] Enriched with MEDDPICC discovery and 4-step objection matrices...</div>`;
        }
        terminalLogs.scrollTop = terminalLogs.scrollHeight;
      }
    }, 1200);

    const res = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    clearInterval(logInterval);

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Failed to generate knowledge base.');
    }

    const data = await res.json();
    latestGeneratedData = data;

    // Complete Progress
    progressBar.style.width = '100%';
    progressPct.innerText = '100%';
    progressTitle.innerText = 'Knowledge Base & Voice Agent Ready!';
    progressSubtitle.innerText = `Successfully ingested ${data.pages_crawled_count} pages and compiled all 3 tiers.`;
    terminalLogs.innerHTML += `<div>[SUCCESS] 3-Tier Zero-Latency Knowledge Bundle generated!</div>`;

    setTimeout(() => {
      progressCard.classList.add('hidden');
    }, 1500);

    // Populate Results
    populateResults(data);

  } catch (err) {
    progressTitle.innerText = 'Error Encountered';
    progressSubtitle.innerText = err.message;
    progressPct.innerText = 'Error';
    progressBar.style.background = '#EF4444';
    terminalLogs.innerHTML += `<div class="text-rose-400">[ERROR] ${err.message}</div>`;
  } finally {
    generateBtn.disabled = false;
    generateBtn.classList.remove('opacity-50', 'cursor-not-allowed');
  }
}

// ---------------------------------------------------------
// Populate Results UI
// ---------------------------------------------------------

function populateResults(data) {
  const kb = data.kb_data;
  const playbook = data.playbook_data;

  // Overview Tab
  document.getElementById('overview-empty-state').classList.add('hidden');
  document.getElementById('overview-populated-grid').classList.remove('hidden');

  document.getElementById('ov-product-name').innerText = kb.product_name || data.product_name;
  document.getElementById('ov-product-tagline').innerText = kb.tagline || data.tagline;
  document.getElementById('ov-pages-badge').innerText = `${data.pages_crawled_count} Pages Ingested`;

  // Pillar 1
  const p1 = document.getElementById('ov-pillar-1');
  const featuresList = (kb.core_specs.features || []).map(f => `<li>• <strong>${f.name}</strong>: ${f.description.slice(0, 80)}...</li>`).join('');
  p1.innerHTML = `<p class="text-slate-400">${kb.core_specs.summary || ''}</p><ul class="mt-1 space-y-0.5">${featuresList}</ul>`;

  // Pillar 2
  const p2 = document.getElementById('ov-pillar-2');
  const plansList = (kb.commercials_pricing.plans || []).map(p => `<li>• <strong>${p.name}</strong>: ${p.price_monthly || p.price_annual || 'Custom'} (${p.best_for || ''})</li>`).join('');
  p2.innerHTML = `<ul class="space-y-0.5">${plansList}</ul><p class="text-[10px] text-amber-400/90 mt-1">Discount Floor: ${kb.commercials_pricing.hard_margin_floor || ''}</p>`;

  // Pillar 3
  const p3 = document.getElementById('ov-pillar-3');
  const personasList = (kb.value_prop_roi.persona_messaging || []).map(pm => `<li>• <strong>${pm.role_title}</strong>: "${pm.tailored_pitch.slice(0, 80)}..."</li>`).join('');
  p3.innerHTML = `<ul class="space-y-0.5">${personasList}</ul>`;

  // Pillar 4
  const p4 = document.getElementById('ov-pillar-4');
  const battlesList = (kb.competitive_intel.battlecards || []).map(b => `<li>• <strong>vs ${b.competitor_name}</strong>: Advantage: ${b.our_distinct_advantages[0] || 'Sub-second speed'}</li>`).join('');
  p4.innerHTML = `<ul class="space-y-0.5">${battlesList}</ul><p class="text-[10px] text-cyan-400 mt-1">Migration: ${kb.competitive_intel.displacement_strategy.migration_timeline_days || '1-2 days'}</p>`;

  // Pillar 5
  const p5 = document.getElementById('ov-pillar-5');
  p5.innerHTML = `<p>• <strong>Timeline:</strong> ${kb.implementation_support.time_to_value_timeline || 'Same-day'}</p><p class="mt-1">• <strong>Prerequisites:</strong> ${(kb.implementation_support.customer_prerequisites || []).join(', ')}</p>`;

  // Pillar 6
  const p6 = document.getElementById('ov-pillar-6');
  p6.innerHTML = `<p>• <strong>Certifications:</strong> ${(kb.security_compliance.certifications || []).join(', ')}</p><p class="mt-1">• <strong>Hosting:</strong> ${kb.security_compliance.data_hosting_provider || 'AWS'}</p><p class="mt-1">• <strong>Encryption:</strong> ${kb.security_compliance.encryption_standards || 'AES-256'}</p>`;

  // Pillar 7
  const p7 = document.getElementById('ov-pillar-7');
  const unsupp = (kb.guardrails_disqualifiers.unsupported_features || []).map(u => `<li>• ❌ ${u}</li>`).join('');
  p7.innerHTML = `<ul class="grid grid-cols-1 md:grid-cols-2 gap-1">${unsupp}</ul>`;

  // Code Tabs
  document.getElementById('code-hot-yaml').textContent = data.tier1_hot_yaml;
  document.getElementById('code-fast-json').textContent = JSON.stringify(data.tier2_fast_json, null, 2);
  document.getElementById('code-edge-md').textContent = data.tier3_edge_md;
  document.getElementById('code-voice-prompt').textContent = data.voice_agent_prompt;

  // Re-run Prism highlight
  if (window.Prism) {
    Prism.highlightAll();
  }
}

// ---------------------------------------------------------
// Tab Switching
// ---------------------------------------------------------

function switchResultTab(tabName) {
  currentResultTab = tabName;

  document.querySelectorAll('.result-tab').forEach(tab => {
    tab.classList.remove('active', 'bg-slate-800', 'text-white');
    tab.classList.add('text-slate-400');
  });

  const activeBtn = document.getElementById(`rtab-${tabName}`);
  if (activeBtn) {
    activeBtn.classList.add('active', 'bg-slate-800', 'text-white');
    activeBtn.classList.remove('text-slate-400');
  }

  document.querySelectorAll('.result-tab-content').forEach(c => c.classList.add('hidden'));
  const activeContent = document.getElementById(`tabcontent-${tabName}`);
  if (activeContent) {
    activeContent.classList.remove('hidden');
  }

  if (window.lucide) {
    lucide.createIcons();
  }
}

// ---------------------------------------------------------
// Copy & Download Utilities
// ---------------------------------------------------------

function getCurrentTabContent() {
  if (!latestGeneratedData) return '';
  if (currentResultTab === 'hot-yaml') return latestGeneratedData.tier1_hot_yaml;
  if (currentResultTab === 'fast-json') return JSON.stringify(latestGeneratedData.tier2_fast_json, null, 2);
  if (currentResultTab === 'edge-md') return latestGeneratedData.tier3_edge_md;
  if (currentResultTab === 'voice-prompt') return latestGeneratedData.voice_agent_prompt;
  if (currentResultTab === 'overview') return JSON.stringify(latestGeneratedData.kb_data, null, 2);
  return '';
}

function copyCurrentTabContent() {
  const content = getCurrentTabContent();
  if (!content) return;
  navigator.clipboard.writeText(content).then(() => {
    const label = document.getElementById('copy-btn-label');
    label.innerText = 'Copied!';
    setTimeout(() => { label.innerText = 'Copy'; }, 1500);
  });
}

function downloadCurrentTabContent() {
  const content = getCurrentTabContent();
  if (!content) return;

  let ext = 'txt';
  let filename = 'voice_agent_knowledge';

  if (currentResultTab === 'hot-yaml') { ext = 'yaml'; filename = 'hot_system_prompt'; }
  else if (currentResultTab === 'fast-json') { ext = 'json'; filename = 'fast_lookup'; }
  else if (currentResultTab === 'edge-md') { ext = 'md'; filename = 'edge_case_kb'; }
  else if (currentResultTab === 'voice-prompt') { ext = 'txt'; filename = 'voice_agent_master_prompt'; }

  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${filename}.${ext}`;
  a.click();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------
// Interactive Voice Simulator
// ---------------------------------------------------------

async function sendSimulatedMessage(msgText) {
  if (!latestGeneratedData) {
    alert('Please generate a knowledge base first!');
    return;
  }
  appendChatMessage('PROSPECT (USER)', msgText, 'bg-slate-800 text-slate-200');

  try {
    const res = await fetch('/api/simulate-voice-turn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_message: msgText,
        kb_data: latestGeneratedData.kb_data,
        playbook_data: latestGeneratedData.playbook_data
      })
    });

    const data = await res.json();
    appendAgentResponse(data.agent_response, data.latency_ms, data.step_executed);
  } catch (err) {
    appendChatMessage('SYSTEM', 'Simulation error: ' + err.message, 'bg-rose-900/50 text-rose-300');
  }
}

function handleSimSubmit(event) {
  event.preventDefault();
  const input = document.getElementById('sim-input');
  const val = input.value.trim();
  if (!val) return;
  input.value = '';
  sendSimulatedMessage(val);
}

function appendChatMessage(sender, text, tagClass) {
  const feed = document.getElementById('sim-chat-feed');
  const div = document.createElement('div');
  div.className = 'flex items-start space-x-2 text-xs animate-fade-in';
  div.innerHTML = `
    <span class="px-2 py-0.5 rounded ${tagClass} font-semibold text-[10px]">${sender}</span>
    <span class="text-slate-200">${text}</span>
  `;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
}

function appendAgentResponse(text, latency, stepExecuted) {
  const feed = document.getElementById('sim-chat-feed');
  const div = document.createElement('div');
  div.className = 'flex flex-col space-y-1 bg-slate-900/90 p-2.5 rounded-xl border border-indigo-500/20 text-xs animate-fade-in';
  div.innerHTML = `
    <div class="flex items-center justify-between">
      <span class="px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-400 font-semibold text-[10px]">AI AGENT (0ms Reflex)</span>
      <span class="text-[10px] font-mono text-emerald-400">${latency} • ${stepExecuted}</span>
    </div>
    <p class="text-slate-100">${text}</p>
  `;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
}
