<template>
  <div>
    <!-- 工具栏 -->
    <div class="toolbar">
      <a-segmented v-model:value="formFilter" :options="formOptions" @change="loadSets" />
      <a-input-search v-model:value="kw" placeholder="搜索套题名称" style="max-width:240px;" allow-clear @search="loadSets" />
      <a-button type="primary" @click="openCreate">+ 新建套题</a-button>
      <a-button @click="openFromResume">🎯 从简历生成套题</a-button>
    </div>

    <!-- 套题表格 -->
    <a-table :data-source="sets" :columns="setCols" row-key="id" size="middle" :loading="loading"
      :expanded-row-keys="expandedKeys" @expandedRowsChange="(k) => (expandedKeys = k)">
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'form_type'">
          <a-tag :color="{ structured: 'blue', group: 'purple', semi: 'cyan' }[record.form_type]">
            {{ { structured: '结构化', group: '群面', semi: '半结构化' }[record.form_type] }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'difficulty'">
          <a-tag :color="{ easy: 'green', medium: 'orange', hard: 'red' }[record.difficulty]">
            {{ { easy: '简单', medium: '中等', hard: '困难' }[record.difficulty] }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'questions'">
          <a-tag v-for="q in (record.questions || []).slice(0, 99)" :key="q.id" style="margin-bottom:2px;">
            {{ q.dimension || '综合' }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'op'">
          <a-space>
            <a-button size="small" @click="openEdit(record)">编辑</a-button>
            <a-popconfirm title="删除该套题及全部题目？" @confirm="removeSet(record)">
              <a-button size="small" danger>删除</a-button>
            </a-popconfirm>
          </a-space>
        </template>
      </template>
      <!-- 展开行：题目列表 -->
      <template #expandedRowRender="{ record }">
        <div class="q-list">
          <div v-for="q in record.questions || []" :key="q.id" class="q-row">
            <span class="q-seq">{{ q.seq }}.</span>
            <div class="q-main">
              <div class="q-content">{{ q.content }}</div>
              <div class="q-meta">
                <a-tag color="geekblue">{{ q.dimension || '综合' }}</a-tag>
                <a-tag v-if="q.category">{{ q.category }}</a-tag>
                <span class="q-time">{{ q.time_limit_s }}s</span>
                <span v-if="q.ref_answer" class="q-ref">参考：{{ q.ref_answer }}</span>
              </div>
            </div>
            <a-popconfirm title="删除该题？" @confirm="removeQuestion(record, q)">
              <a-button size="small" type="text" danger>删除</a-button>
            </a-popconfirm>
          </div>
          <a-empty v-if="!(record.questions || []).length" description="暂无题目" />
        </div>
      </template>
    </a-table>

    <!-- 新建/编辑抽屉 -->
    <a-drawer v-model:open="drawerOpen" :title="editId ? '编辑套题' : '新建套题'" width="720" destroy-on-close>
      <a-form layout="vertical">
        <div class="form-grid">
          <a-form-item label="套题名称" required>
            <a-input v-model:value="form.name" placeholder="如：运营岗结构化题库" />
          </a-form-item>
          <a-form-item label="岗位方向">
            <a-input v-model:value="form.position_type" placeholder="如：运营 / 数据分析" />
          </a-form-item>
          <a-form-item label="面试形式" required>
            <a-select v-model:value="form.form_type">
              <a-select-option value="structured">结构化</a-select-option>
              <a-select-option value="group">群面</a-select-option>
              <a-select-option value="semi">半结构化</a-select-option>
            </a-select>
          </a-form-item>
          <a-form-item label="难度" required>
            <a-select v-model:value="form.difficulty">
              <a-select-option value="easy">简单</a-select-option>
              <a-select-option value="medium">中等</a-select-option>
              <a-select-option value="hard">困难</a-select-option>
            </a-select>
          </a-form-item>
        </div>
        <a-form-item label="描述">
          <a-textarea v-model:value="form.description" :rows="2" />
        </a-form-item>

        <div class="q-editor-head">
          <span>题目列表（{{ form.questions.length }} 题）</span>
          <a-button size="small" type="dashed" @click="addQuestion">+ 添加题目</a-button>
        </div>
        <div v-for="(q, i) in form.questions" :key="i" class="q-edit-row">
          <div class="qe-line1">
            <span class="qe-seq">{{ i + 1 }}.</span>
            <a-input v-model:value="q.content" placeholder="题目内容" />
            <a-button size="small" type="text" danger @click="form.questions.splice(i, 1)">✕</a-button>
          </div>
          <div class="qe-line2">
            <a-select v-model:value="q.dimension" style="width:130px;" placeholder="考察维度">
              <a-select-option v-for="d in dims" :key="d" :value="d">{{ d }}</a-select-option>
            </a-select>
            <a-input v-model:value="q.category" style="width:120px;" placeholder="类别" />
            <a-input-number v-model:value="q.time_limit_s" :min="30" :max="600" :step="30" addon-after="s" style="width:120px;" />
            <a-input v-model:value="q.ref_answer" placeholder="参考答案（选填）" style="flex:1;" />
          </div>
        </div>
      </a-form>
      <template #footer>
        <a-space>
          <a-button @click="drawerOpen = false">取消</a-button>
          <a-button type="primary" :loading="saving" @click="saveSet">保存</a-button>
        </a-space>
      </template>
    </a-drawer>

    <!-- 从简历生成套题 -->
    <a-modal v-model:open="fromResumeOpen" title="从简历 AI 生成面试套题" @ok="confirmFromResume" :confirm-loading="genLoading">
      <p class="modal-tip">选择一份已解析的简历，AI 将基于画像中的项目/技能/量化数据生成「简历深挖」题目并入库（半结构化形式）。</p>
      <a-select v-model:value="fromResumeId" style="width:100%;" placeholder="选择简历" :loading="resumesLoading" show-search option-filter-prop="label">
        <a-select-option v-for="r in resumeOptions" :key="r.value" :value="r.value" :label="r.label">
          {{ r.label }}
        </a-select-option>
      </a-select>
      <div style="margin-top:12px;">
        <span>题目数量：</span>
        <a-input-number v-model:value="fromResumeCount" :min="4" :max="8" />
      </div>
    </a-modal>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { adminApi } from '@/api'

const dims = ['专业深度', '逻辑结构', '语言表达', '应急应变', '岗位匹配']
const formOptions = [
  { label: '全部', value: '' },
  { label: '结构化', value: 'structured' },
  { label: '群面', value: 'group' },
  { label: '半结构化', value: 'semi' },
]

const sets = ref([])
const loading = ref(false)
const formFilter = ref('')
const kw = ref('')
const expandedKeys = ref([])

const setCols = [
  { title: 'ID', dataIndex: 'id', key: 'id', width: 60 },
  { title: '套题名称', dataIndex: 'name', key: 'name' },
  { title: '形式', key: 'form_type', width: 100 },
  { title: '难度', key: 'difficulty', width: 80 },
  { title: '岗位方向', dataIndex: 'position_type', key: 'position_type', customRender: ({ text }) => text || '—' },
  { title: '题数', dataIndex: 'question_count', key: 'question_count', width: 70 },
  { title: '约需', dataIndex: 'est_minutes', key: 'est_minutes', width: 70, customRender: ({ text }) => `${text} 分钟` },
  { title: '维度覆盖', key: 'questions' },
  { title: '操作', key: 'op', width: 140 },
]

const drawerOpen = ref(false)
const editId = ref(null)
const saving = ref(false)
const form = ref({ name: '', form_type: 'structured', difficulty: 'medium', description: '', position_type: '', industry: '', questions: [] })

const fromResumeOpen = ref(false)
const fromResumeId = ref(null)
const fromResumeCount = ref(5)
const genLoading = ref(false)
const resumeOptions = ref([])
const resumesLoading = ref(false)

function blankQuestion() {
  return { content: '', dimension: '专业深度', category: '', time_limit_s: 180, ref_answer: '' }
}

async function loadSets() {
  loading.value = true
  try {
    sets.value = await adminApi.questionSets({ form_type: formFilter.value, keyword: kw.value }) || []
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editId.value = null
  form.value = { name: '', form_type: 'structured', difficulty: 'medium', description: '', position_type: '', industry: '', questions: [blankQuestion(), blankQuestion()] }
  drawerOpen.value = true
}

function openEdit(record) {
  editId.value = record.id
  form.value = {
    name: record.name, form_type: record.form_type, difficulty: record.difficulty,
    description: record.description, position_type: record.position_type, industry: record.industry,
    questions: (record.questions || []).map(q => ({
      content: q.content, dimension: q.dimension || '专业深度', category: q.category,
      time_limit_s: q.time_limit_s, ref_answer: q.ref_answer,
    })),
  }
  drawerOpen.value = true
}

function addQuestion() {
  form.value.questions.push(blankQuestion())
}

async function saveSet() {
  const f = form.value
  if (!f.name?.trim()) { message.warning('请填写套题名称'); return }
  if (!f.questions.some(q => q.content?.trim())) { message.warning('至少填写 1 道题目内容'); return }
  saving.value = true
  try {
    const payload = { ...f, questions: f.questions.filter(q => q.content?.trim()) }
    if (editId.value) await adminApi.updateQuestionSet(editId.value, payload)
    else await adminApi.createQuestionSet(payload)
    message.success(editId.value ? '套题已更新' : '套题已创建')
    drawerOpen.value = false
    loadSets()
  } finally {
    saving.value = false
  }
}

async function removeSet(record) {
  await adminApi.deleteQuestionSet(record.id)
  message.success('已删除')
  loadSets()
}

async function removeQuestion(record, q) {
  await adminApi.deleteQuestion(q.id)
  message.success('题目已删除')
  loadSets()
}

async function openFromResume() {
  fromResumeOpen.value = true
  resumesLoading.value = true
  try {
    const r = await adminApi.resumes({ page: 1, page_size: 50 })
    resumeOptions.value = (r?.list || []).map(x => ({
      value: x.id,
      label: `#${x.id} ${x.candidate_name || '未识别'} · ${x.filename || ''} · ${x.target_position || ''}`,
    }))
  } finally {
    resumesLoading.value = false
  }
}

async function confirmFromResume() {
  if (!fromResumeId.value) { message.warning('请选择简历'); return }
  genLoading.value = true
  try {
    const created = await adminApi.createSetFromResume({ resume_id: fromResumeId.value, count: fromResumeCount.value })
    message.success(`已生成套题「${created?.name || ''}」（${created?.question_count || 0} 题）`)
    fromResumeOpen.value = false
    loadSets()
  } finally {
    genLoading.value = false
  }
}

onMounted(loadSets)
</script>

<style scoped>
.toolbar { margin-bottom: 12px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.q-list { padding: 4px 12px; }
.q-row { display: flex; align-items: flex-start; gap: 8px; padding: 8px 0; border-bottom: 1px dashed var(--border-soft); }
.q-row:last-child { border-bottom: none; }
.q-seq { font-weight: 700; color: var(--primary); }
.q-main { flex: 1; }
.q-content { font-size: 13px; }
.q-meta { margin-top: 4px; display: flex; gap: 6px; align-items: center; font-size: 12px; color: var(--text-3); flex-wrap: wrap; }
.q-time { color: var(--text-3); }
.q-ref { color: var(--text-3); }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
.q-editor-head { display: flex; justify-content: space-between; align-items: center; margin: 8px 0; font-weight: 600; }
.q-edit-row { padding: 8px; border: 1px solid var(--border-soft); border-radius: 8px; margin-bottom: 8px; }
.qe-line1 { display: flex; gap: 8px; align-items: center; }
.qe-seq { font-weight: 700; color: var(--primary); }
.qe-line2 { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.modal-tip { font-size: 13px; color: var(--text-2); }
</style>
