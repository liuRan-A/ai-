<template>
  <div class="outline-wrap">
    <!-- 左：生成 + 列表 -->
    <div class="outline-left">
      <div class="mw-card gen-card">
        <div class="card-title">🎯 生成结构化面试提纲</div>
        <p class="gen-tip">粘贴岗位 JD（可同时选择候选人简历），AI 将生成统一评估框架（维度 / 权重 / 题目 / 追问预案），确保所有候选人面对同一套标准。</p>
        <a-textarea v-model:value="jdText" :rows="5" placeholder="粘贴岗位 JD，如：&#10;岗位职责：负责用户增长策略设计与落地…&#10;任职要求：3年以上增长经验，熟悉数据分析…" />
        <div class="gen-row">
          <a-select v-model:value="resumeId" style="flex:1;" placeholder="关联候选人简历（可选）" allow-clear
            :loading="resumesLoading" show-search option-filter-prop="label">
            <a-select-option v-for="r in resumeOptions" :key="r.value" :value="r.value" :label="r.label">{{ r.label }}</a-select-option>
          </a-select>
          <a-button type="primary" :loading="generating" @click="generate">AI 生成提纲</a-button>
        </div>
        <a-tag v-if="lastEngine" :color="lastEngine === 'deepseek' ? 'blue' : 'default'" style="margin-top:8px;">
          引擎：{{ lastEngine === 'deepseek' ? 'DeepSeek' : '规则引擎' }}
        </a-tag>
      </div>

      <div class="mw-card" style="margin-top:14px;">
        <div class="card-title">历史提纲（{{ outlines.length }}）</div>
        <div class="outline-list">
          <div v-for="o in outlines" :key="o.id" class="outline-item"
            :class="{ active: current?.id === o.id }" @click="selectOutline(o)">
            <div class="oi-title">{{ o.title || '未命名提纲' }}</div>
            <div class="oi-meta">
              <a-tag :color="o.engine === 'deepseek' ? 'blue' : 'default'" style="margin-right:6px;">
                {{ o.engine === 'deepseek' ? 'AI' : '规则' }}
              </a-tag>
              <span>{{ (o.outline?.dimensions || []).length }} 个维度</span>
              <span class="oi-time">{{ o.created_at }}</span>
            </div>
          </div>
          <a-empty v-if="!outlines.length" description="暂无提纲，先生成一份" />
        </div>
      </div>
    </div>

    <!-- 右：提纲详情 -->
    <div class="outline-right">
      <div class="mw-card detail-card">
        <template v-if="current">
          <div class="detail-head">
            <div>
              <div class="detail-title">{{ current.title || '面试提纲' }}</div>
              <div class="detail-sub">{{ current.outline?.summary }}</div>
            </div>
          </div>
          <div class="dim-block" v-for="(d, i) in current.outline?.dimensions || []" :key="i">
            <div class="dim-head">
              <span class="dim-name">{{ d.name }}</span>
              <div class="dim-weight-shell">
                <span class="dim-weight-bar" :style="{ width: (d.weight || 0) + '%' }"></span>
              </div>
              <span class="dim-weight-num">{{ d.weight }}%</span>
            </div>
            <div v-for="(q, j) in d.questions || []" :key="j" class="oq-row">
              <div class="oq-content">{{ j + 1 }}. {{ q.content }}</div>
              <div class="oq-ref" v-if="q.ref_answer">采分点：{{ q.ref_answer }}</div>
              <div class="oq-followups" v-if="q.followups?.length">
                <a-tag v-for="(f, k) in q.followups" :key="k" color="orange" style="margin-bottom:2px;">追问：{{ f }}</a-tag>
              </div>
            </div>
          </div>
        </template>
        <a-empty v-else description="选择左侧提纲查看详情" style="margin-top:80px;" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { adminApi } from '@/api'

const outlines = ref([])
const current = ref(null)
const jdText = ref('')
const resumeId = ref(null)
const generating = ref(false)
const lastEngine = ref('')
const resumeOptions = ref([])
const resumesLoading = ref(false)

async function loadOutlines() {
  outlines.value = await adminApi.outlines().catch(() => []) || []
  if (!current.value && outlines.value.length) current.value = outlines.value[0]
}

async function loadResumes() {
  resumesLoading.value = true
  try {
    const r = await adminApi.resumes({ page: 1, page_size: 50 })
    resumeOptions.value = (r?.list || []).map(x => ({
      value: x.id,
      label: `#${x.id} ${x.candidate_name || '未识别'} · ${x.target_position || x.filename || ''}`,
    }))
  } finally {
    resumesLoading.value = false
  }
}

async function generate() {
  if (!jdText.value.trim() && !resumeId.value) {
    message.warning('请粘贴 JD 或选择简历')
    return
  }
  generating.value = true
  try {
    const created = await adminApi.generateOutline({ jd_text: jdText.value, resume_id: resumeId.value })
    lastEngine.value = created?.engine || ''
    message.success('提纲已生成')
    jdText.value = ''
    await loadOutlines()
    current.value = created || outlines.value[0]
  } finally {
    generating.value = false
  }
}

function selectOutline(o) {
  current.value = o
}

onMounted(() => { loadOutlines(); loadResumes() })
</script>

<style scoped>
.outline-wrap { display: grid; grid-template-columns: 360px 1fr; gap: 14px; }
.gen-card { padding: 16px; }
.card-title { font-size: 14px; font-weight: 700; margin-bottom: 10px; }
.gen-tip { font-size: 12px; color: var(--text-3); margin-bottom: 10px; }
.gen-row { display: flex; gap: 10px; margin-top: 10px; }
.outline-list { display: flex; flex-direction: column; gap: 8px; max-height: 420px; overflow-y: auto; }
.outline-item { padding: 10px 12px; border: 1px solid var(--border-soft); border-radius: 10px; cursor: pointer; }
.outline-item.active { border-color: var(--primary); background: rgba(62, 99, 221, 0.05); }
.oi-title { font-size: 13px; font-weight: 600; }
.oi-meta { margin-top: 6px; font-size: 12px; color: var(--text-3); display: flex; align-items: center; gap: 8px; }
.oi-time { margin-left: auto; }
.detail-card { padding: 18px; min-height: 480px; }
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
.detail-title { font-size: 17px; font-weight: 800; }
.detail-sub { font-size: 13px; color: var(--text-2); margin-top: 4px; max-width: 640px; }
.dim-block { margin-bottom: 16px; }
.dim-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.dim-name { font-weight: 700; font-size: 14px; width: 90px; }
.dim-weight-shell { flex: 1; height: 8px; background: var(--border-soft); border-radius: 4px; overflow: hidden; }
.dim-weight-bar { display: block; height: 100%; background: linear-gradient(90deg, #3E63DD, #7C6BD6); border-radius: 4px; }
.dim-weight-num { font-size: 12px; color: var(--text-2); width: 40px; text-align: right; }
.oq-row { padding: 8px 12px; background: var(--bg); border-radius: 8px; margin-bottom: 6px; }
.oq-content { font-size: 13px; }
.oq-ref { font-size: 12px; color: var(--text-3); margin-top: 4px; }
.oq-followups { margin-top: 4px; }
@media (max-width: 960px) { .outline-wrap { grid-template-columns: 1fr; } }
</style>
