import http from './http'

export const sessionApi = {
  create: (payload) => http.post('/sessions', payload),
  get: (sessionId) => http.get(`/sessions/${sessionId}`),
  submitAnswer: (sessionId, sqId, payload) =>
    http.post(`/sessions/${sessionId}/questions/${sqId}/answer`, payload),
  finish: (sessionId) => http.post(`/sessions/${sessionId}/finish`),
  questionDetail: (sessionId, sqId) =>
    http.get(`/sessions/${sessionId}/questions/${sqId}`),
  feedback: (sqId, payload) =>
    http.post(`/sessions/questions/${sqId}/feedback`, payload),
  peers: (sessionId) => http.get(`/sessions/${sessionId}/peers`),
  peerTalk: (sessionId, payload) =>
    http.post(`/sessions/${sessionId}/peer-talk`, payload),
  // 群面多智能体导演：事件驱动，后端决定谁发言/回应谁/主持人控场
  groupDirect: (sessionId, payload) =>
    http.post(`/sessions/${sessionId}/group/direct`, payload),
  uploadVideo: (sessionId, sqId, blob) => {
    const fd = new FormData()
    fd.append('file', blob, /mp4/i.test(blob.type || '') ? 'answer.mp4' : 'answer.webm')
    return http.post(`/sessions/${sessionId}/questions/${sqId}/video`, fd)
  },
}
