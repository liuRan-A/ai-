<template>
  <div class="voice-wrap">
    <!-- 左：候选人列表 -->
    <div class="mw-card voice-left">
      <div class="card-title">🎤 候选人语音分析</div>
      <p class="voice-tip">基于语音指标（语速 / 停顿 / 填充词 / 打断）与作答转写，分析候选人的情绪状态、抗压能力与语言表达素养。</p>
      <a-input-search v-model:value="kw" placeholder="搜索昵称/手机号" allow-clear @search="loadUsers" style="margin-bottom:10px;" />
      <div class="user-list">
        <div v-for="u in users" :key="u.id" class="user-item" :class="{ active: currentUserId === u.id }"
          @click="selectUser(u)">
          <div class="ui-name">{{ u.nickname }}</div>
          <div class="ui-meta">
            <span>{{ u.phone || '—' }}</span>
            <a-tag :color="u.answered_count ? 'blue' : 'default'" style="margin-left:auto;">{{ u.answered_count }} 次作答</a-tag>
          </div>
        </div>
        <a-empty v-if="!users.length" description="暂无用户" />
      </div>
    </div>

    <!-- 右：分析结果 -->
    <div class="voice-right">
      <a-spin :spinning="loading">
        <template v-if="profile">
          <div class="mw-card" style="padding:18px;">
            <div class="pf-head">
              <div class="pf-title">{{ currentUser?.nickname }} 的语音画像</div>
              <a-tag :color="profile.engine === 'deepseek' ? 'blue' : 'default'">
                {{ profile.engine === 'deepseek' ? 'AI 分析' : '指标规则分析' }}
              </a-tag>
            </div>

            <div class="pf-grid">
              <div class="pf-card">
                <div class="pf-label">情绪状态</div>
                <div class="pf-value">{{ profile.emotion }}</div>
                <div class="pf-detail">{{ profile.emotion_detail }}</div>
              </div>
              <div class="pf-card">
                <div class="pf-label">抗压能力</div>
                <div class="pf-value">
                  <a-tag :color="{ strong: 'green', medium: 'orange', weak: 'red' }[profile.resilience]">
                    {{ { strong: '较强', medium: '中等', weak: '偏弱' }[profile.resilience] }}
                  </a-tag>
                </div>
                <div class="pf-detail">{{ profile.resilience_detail }}</div>
              </div>
              <div class="pf-card">
                <div class="pf-label">语言素养</div>
                <div class="pf-value">
                  <a-tag :color="{ strong: 'green', medium: 'orange', weak: 'red' }[profile.expression]">
                    {{ { strong: '较强', medium: '中等', weak: '偏弱' }[profile.expression] }}
                  </a-tag>
                </div>
                <div class="pf-detail">{{ profile.expression_detail }}</div>
              </div>
              <div class="pf-card">
                <div class="pf-label">语音综合分</div>
                <div class="pf-value big" :style="{ color: scoreColor(profile.voice_score) }">{{ profile.voice_score }}</div>
                <div class="pf-detail">语速 {{ profile.metrics_avg?.wpm }} 字/分 · 停顿 {{ profile.metrics_avg?.pause_count }} 次/题</div>
              </div>
            </div>

            <div v-if="profile.suggestions?.length" class="pf-suggest">
              <div class="pf-sug-title">💡 面试官参考建议</div>
              <div v-for="(s, i) in profile.suggestions" :key="i" class="pf-sug-item">· {{ s }}</div>
            </div>
          </div>

          <div class="mw-card" style="margin-top:14px; padding:14px 18px;">
            <div class="card-title">逐题语音指标（{{ questions.length }} 题）</div>
            <a-table :data-source="questions" :columns="qCols" row-key="key" size="small" :pagination="questions.length > 8 ? { pageSize: 8 } : false">
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'question'">
                  <span class="qq-content">{{ record.form_label }} #{{ record.seq }} {{ record.question_content || '（未记录题目）' }}</span>
                </template>
                <template v-else-if="column.key === 'duration'">{{ fmtMs(record.duration_ms) }}</template>
                <template v-else-if="column.key === 'pause'">{{ record.pause_count }} 次 / {{ Math.round(record.pause_ms_total / 1000) }}s</template>
                <template v-else-if="column.key === 'transcript'">
                  <a-tooltip :title="record.transcript">
                    <span class="qq-tr">{{ record.transcript ? record.transcript.slice(0, 18) + '…' : '—' }}</span>
                  </a-tooltip>
                </template>
              </template>
            </a-table>
          </div>
        </template>
        <div v-else class="mw-card empty-card">
          <a-empty description="选择左侧候选人查看语音分析" style="margin-top:100px;" />
        </div>
      </a-spin>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { adminApi } from '@/api'

const users = ref([])
const kw = ref('')
const currentUserId = ref(null)
const currentUser = ref(null)
const loading = ref(false)
const profile = ref(null)
const questions = ref([])

const qCols = [
  { title: '题目', key: 'question' },
  { title: '语速', dataIndex: 'wpm', key: 'wpm', width: 80, customRender: ({ text }) => `${text}/分` },
  { title: '停顿', key: 'pause', width: 110 },
  { title: '填充词', dataIndex: 'filler_count', key: 'filler_count', width: 70 },
  { title: '打断', dataIndex: 'interrupt_count', key: 'interrupt_count', width: 60 },
  { title: '时长', key: 'duration', width: 80 },
  { title: '转写', key: 'transcript', width: 130 },
]

function scoreColor(s) { return s >= 80 ? '#2FA36B' : s >= 65 ? '#E8912D' : '#D64545' }
function fmtMs(ms) {
  ms = ms || 0
  const s = Math.round(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

async function loadUsers() {
  users.value = await adminApi.candidateUsers({ keyword: kw.value }).catch(() => []) || []
}

async function selectUser(u) {
  currentUserId.value = u.id
  currentUser.value = u
  loading.value = true
  profile.value = null
  questions.value = []
  try {
    const r = await adminApi.voiceAnalysis(u.id)
    profile.value = r?.profile || null
    questions.value = (r?.questions || []).map((q, i) => ({ ...q, key: `${q.session_id}-${i}` }))
    if (!r?.question_count) message.info('该用户暂无作答语音数据')
  } finally {
    loading.value = false
  }
}

onMounted(loadUsers)
</script>

<style scoped>
.voice-wrap { display: grid; grid-template-columns: 300px 1fr; gap: 14px; }
.voice-left { padding: 16px; }
.card-title { font-size: 14px; font-weight: 700; margin-bottom: 10px; }
.voice-tip { font-size: 12px; color: var(--text-3); margin-bottom: 10px; }
.user-list { display: flex; flex-direction: column; gap: 8px; max-height: 520px; overflow-y: auto; }
.user-item { padding: 10px 12px; border: 1px solid var(--border-soft); border-radius: 10px; cursor: pointer; }
.user-item.active { border-color: var(--primary); background: rgba(62, 99, 221, 0.05); }
.ui-name { font-size: 13px; font-weight: 600; }
.ui-meta { margin-top: 4px; font-size: 12px; color: var(--text-3); display: flex; align-items: center; gap: 8px; }
.pf-head { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.pf-title { font-size: 16px; font-weight: 800; }
.pf-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.pf-card { padding: 14px; background: var(--bg); border-radius: 10px; }
.pf-label { font-size: 12px; color: var(--text-3); }
.pf-value { margin-top: 6px; font-size: 18px; font-weight: 800; }
.pf-value.big { font-size: 26px; }
.pf-detail { margin-top: 6px; font-size: 11px; color: var(--text-3); line-height: 1.5; min-height: 32px; }
.pf-suggest { margin-top: 14px; padding: 12px 14px; background: rgba(232, 145, 45, 0.08); border-radius: 10px; }
.pf-sug-title { font-weight: 700; font-size: 13px; margin-bottom: 6px; }
.pf-sug-item { font-size: 13px; color: var(--text-2); margin-bottom: 4px; }
.qq-content { font-size: 12px; }
.qq-tr { font-size: 12px; color: var(--text-3); }
.empty-card { min-height: 480px; }
@media (max-width: 960px) { .voice-wrap { grid-template-columns: 1fr; } .pf-grid { grid-template-columns: repeat(2, 1fr); } }
</style>
