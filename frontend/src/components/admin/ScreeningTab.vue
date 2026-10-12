<template>
  <div>
    <!-- 筛选工具区 -->
    <div class="mw-card screen-card">
      <div class="card-title">📋 按岗位 JD 批量筛选简历</div>
      <a-textarea v-model:value="jdText" :rows="4" placeholder="粘贴岗位 JD，如：&#10;岗位职责：负责数据中台建设…&#10;任职要求：5年以上数据经验，高级/资深级别…" />
      <div class="screen-row">
        <a-select v-model:value="selectedResumeIds" mode="multiple" style="flex:1;" placeholder="选择候选人简历（可多选，最多 20 份）"
          :loading="resumesLoading" show-search option-filter-prop="label" :max-tag-count="3">
          <a-select-option v-for="r in resumeOptions" :key="r.value" :value="r.value" :label="r.label">{{ r.label }}</a-select-option>
        </a-select>
        <a-button type="primary" :loading="running" @click="runScreening">开始筛选</a-button>
      </div>
    </div>

    <!-- 筛选结果 -->
    <div class="mw-card" style="margin-top:14px;">
      <div class="result-head">
        <div class="card-title">筛选结果（按匹配分排序）</div>
        <a-space>
          <span class="sel-hint">已选 {{ checkedIds.length }}/5 人</span>
          <a-button :disabled="checkedIds.length < 2" type="primary" @click="openCompare">
            ⚖️ 终面对比（雷达图）
          </a-button>
        </a-space>
      </div>

      <a-table :data-source="results" :columns="resultCols" row-key="id" size="middle" :loading="listLoading"
        :pagination="results.length > 10 ? { pageSize: 10 } : false"
        :row-selection="{ selectedRowKeys: checkedIds, onChange: onCheck }">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'rank'">
            <span class="rank-badge" :class="'r' + (results.indexOf(record) + 1)">{{ results.indexOf(record) + 1 }}</span>
          </template>
          <template v-else-if="column.key === 'score'">
            <span class="score-num" :style="{ color: scoreColor(record.score) }">{{ record.score }}</span>
          </template>
          <template v-else-if="column.key === 'verdict'">
            <a-tag :color="{ recommend: 'green', consider: 'orange', reject: 'red' }[record.verdict]">
              {{ { recommend: '推荐', consider: '待定', reject: '不推荐' }[record.verdict] }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'concerns'">
            <template v-if="record.concerns?.length">
              <a-tooltip v-for="(c, i) in record.concerns" :key="i" :title="c.text">
                <a-tag :color="concernColor(c.type)" style="margin-bottom:2px; cursor: default;">
                  {{ concernLabel(c.type) }} {{ c.text?.slice(0, 14) }}…
                </a-tag>
              </a-tooltip>
            </template>
            <span v-else class="no-concern">✓ 无明显疑点</span>
          </template>
          <template v-else-if="column.key === 'engine'">
            <a-tag :color="record.engine === 'deepseek' ? 'blue' : 'default'">{{ record.engine === 'deepseek' ? 'AI' : '规则' }}</a-tag>
          </template>
          <template v-else-if="column.key === 'op'">
            <a-button size="small" type="link" @click="showDetail(record)">详情</a-button>
          </template>
        </template>
      </a-table>
    </div>

    <!-- 详情弹窗 -->
    <a-modal v-model:open="detailOpen" :title="detailRow ? `${detailRow.candidate_name || '候选人'} · 匹配 ${detailRow.score} 分` : '详情'" :footer="null" width="640">
      <template v-if="detailRow">
        <p class="d-summary">{{ detailRow.summary }}</p>
        <div v-if="detailRow.concerns?.length">
          <div class="d-sec">⚠️ 疑点与追问</div>
          <div v-for="(c, i) in detailRow.concerns" :key="i" class="d-concern">
            <a-tag :color="concernColor(c.type)">{{ concernLabel(c.type) }}</a-tag>
            <span class="d-concern-text">{{ c.text }}</span>
            <div class="d-followup" v-if="c.followup">↳ 追问：{{ c.followup }}</div>
          </div>
        </div>
        <div v-if="detailRow.followups?.length">
          <div class="d-sec">📝 面试前预准备追问</div>
          <ol class="d-fu-list">
            <li v-for="(f, i) in detailRow.followups" :key="i">{{ f }}</li>
          </ol>
        </div>
      </template>
    </a-modal>

    <!-- 对比弹窗 -->
    <a-modal v-model:open="compareOpen" title="终面候选人多维度对比" :footer="null" width="860">
      <a-spin :spinning="comparing">
        <RadarChart v-if="compareData" :indicators="compareData.indicators"
          :series="compareData.candidates.map(c => ({ name: c.name, values: c.values }))" />
        <table v-if="compareData" class="cmp-table">
          <thead>
            <tr>
              <th>候选人</th>
              <th v-for="ind in compareData.indicators" :key="ind.name">{{ ind.name }}</th>
              <th>面试均分</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in compareData.candidates" :key="c.id">
              <td class="cmp-name">
                <a-tag :color="{ recommend: 'green', consider: 'orange', reject: 'red' }[c.verdict]">
                  {{ { recommend: '推荐', consider: '待定', reject: '不推荐' }[c.verdict] }}
                </a-tag>
                {{ c.name }}
              </td>
              <td v-for="(v, i) in c.values" :key="i">{{ v }}</td>
              <td>{{ c.interview_avg || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p class="cmp-note" v-if="compareData">* 「筛选匹配」来自简历 JD 匹配分；面试 5 维为该候选人全部已完成场次的评分均值（无面试数据时以 50 分中性值参与对比）。</p>
      </a-spin>
    </a-modal>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { adminApi } from '@/api'
import RadarChart from '@/components/RadarChart.vue'

const jdText = ref('')
const resumeOptions = ref([])
const resumesLoading = ref(false)
const selectedResumeIds = ref([])
const running = ref(false)

const results = ref([])
const listLoading = ref(false)
const checkedIds = ref([])

const detailOpen = ref(false)
const detailRow = ref(null)

const compareOpen = ref(false)
const comparing = ref(false)
const compareData = ref(null)

const resultCols = [
  { title: '#', key: 'rank', width: 50 },
  { title: '候选人', dataIndex: 'candidate_name', key: 'candidate_name', customRender: ({ record }) => record.candidate_name || record.user_name || '未识别' },
  { title: '目标岗位', dataIndex: 'target_position', key: 'target_position', customRender: ({ text }) => text || '—' },
  { title: '匹配分', key: 'score', width: 80, sorter: (a, b) => a.score - b.score },
  { title: '结论', key: 'verdict', width: 90 },
  { title: '疑点', key: 'concerns' },
  { title: '引擎', key: 'engine', width: 70 },
  { title: '操作', key: 'op', width: 70 },
]

const CONCERN_LABELS = { gap: '空窗期', rank_mismatch: '职级不符', frequency: '跳槽频繁', other: '存疑' }
function concernLabel(t) { return CONCERN_LABELS[t] || '存疑' }
function concernColor(t) { return { gap: 'red', rank_mismatch: 'volcano', frequency: 'orange', other: 'gold' }[t] || 'gold' }
function scoreColor(s) { return s >= 75 ? '#2FA36B' : s >= 55 ? '#E8912D' : '#D64545' }

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

async function loadHistory() {
  listLoading.value = true
  try {
    const r = await adminApi.screeningList({ page: 1, page_size: 50 })
    results.value = r?.list || []
  } finally {
    listLoading.value = false
  }
}

async function runScreening() {
  if (!jdText.value.trim()) { message.warning('请粘贴岗位 JD'); return }
  if (!selectedResumeIds.value.length) { message.warning('请选择至少一份简历'); return }
  running.value = true
  try {
    const r = await adminApi.screeningRun({ jd_text: jdText.value, resume_ids: selectedResumeIds.value })
    message.success(`已筛选 ${r?.count || 0} 份简历`)
    await loadHistory()
  } finally {
    running.value = false
  }
}

function onCheck(keys) {
  checkedIds.value = keys.slice(0, 5)
  if (keys.length > 5) message.warning('最多选择 5 名候选人对比')
}

function showDetail(record) {
  detailRow.value = record
  detailOpen.value = true
}

async function openCompare() {
  compareOpen.value = true
  comparing.value = true
  compareData.value = null
  try {
    compareData.value = await adminApi.screeningCompare({ screening_ids: checkedIds.value })
  } finally {
    comparing.value = false
  }
}

onMounted(() => { loadResumes(); loadHistory() })
</script>

<style scoped>
.screen-card { padding: 16px; }
.card-title { font-size: 14px; font-weight: 700; margin-bottom: 10px; }
.screen-row { display: flex; gap: 10px; margin-top: 10px; }
.result-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.sel-hint { font-size: 12px; color: var(--text-3); }
.rank-badge { display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; border-radius: 6px; background: var(--border-soft); font-weight: 700; font-size: 12px; }
.rank-badge.r1 { background: #FFD666; }
.rank-badge.r2 { background: #E6E6E6; }
.rank-badge.r3 { background: #F0C8A0; }
.score-num { font-size: 16px; font-weight: 800; }
.no-concern { font-size: 12px; color: var(--text-3); }
.d-summary { font-size: 13px; color: var(--text-2); }
.d-sec { font-weight: 700; margin: 12px 0 8px; }
.d-concern { padding: 6px 0; border-bottom: 1px dashed var(--border-soft); }
.d-concern-text { font-size: 13px; }
.d-followup { font-size: 12px; color: #E8912D; margin-top: 4px; margin-left: 52px; }
.d-fu-list { padding-left: 20px; font-size: 13px; }
.d-fu-list li { margin-bottom: 4px; }
.cmp-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
.cmp-table th, .cmp-table td { border: 1px solid var(--border-soft); padding: 7px 10px; text-align: center; }
.cmp-table th { background: var(--bg); font-weight: 600; }
.cmp-name { white-space: nowrap; }
.cmp-note { font-size: 12px; color: var(--text-3); margin-top: 10px; }
</style>
