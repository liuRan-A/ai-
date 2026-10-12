import http from './http'

export const adminApi = {
  stats: () => http.get('/admin/stats'),
  users: (params) => http.get('/admin/users', { params }),
  setUserStatus: (userId, status) =>
    http.patch(`/admin/users/${userId}/status`, null, { params: { status } }),
  sessions: (params) => http.get('/admin/sessions', { params }),
  resumes: (params) => http.get('/admin/resumes', { params }),
  // —— 面试回放（管理后台专用） ——
  records: (params) => http.get('/admin/records', { params }),
  sessionDetail: (sessionId) => http.get(`/admin/sessions/${sessionId}/detail`),
  // —— 招聘管理：题库 / 提纲 / 筛选 / 语音分析 ——
  questionSets: (params) => http.get('/admin/question-sets', { params }),
  createQuestionSet: (data) => http.post('/admin/question-sets', data),
  updateQuestionSet: (id, data) => http.put(`/admin/question-sets/${id}`, data),
  deleteQuestionSet: (id) => http.delete(`/admin/question-sets/${id}`),
  updateQuestion: (id, data) => http.put(`/admin/questions/${id}`, data),
  deleteQuestion: (id) => http.delete(`/admin/questions/${id}`),
  createSetFromResume: (data) => http.post('/admin/question-sets/from-resume', data),
  outlines: () => http.get('/admin/outlines'),
  generateOutline: (data) => http.post('/admin/outlines/generate', data),
  screeningRun: (data) => http.post('/admin/screening/run', data, { timeout: 120000 }),
  screeningList: (params) => http.get('/admin/screening', { params }),
  deleteScreening: (id) => http.delete(`/admin/screening/${id}`),
  screeningCompare: (data) => http.post('/admin/screening/compare', data),
  voiceAnalysis: (userId) => http.get('/admin/voice-analysis', { params: { user_id: userId }, timeout: 60000 }),
  candidateUsers: (params) => http.get('/admin/recruit/overview', { params }),
}
