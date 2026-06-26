const {
  API_BASE_URL,
  CLOUD_ENV,
  CLOUD_SERVICE
} = require("../config")

function absoluteUrl(path) {
  if (!path) return ""
  if (/^https?:\/\//.test(path)) return path
  const baseUrl = API_BASE_URL.replace(/\/+$/, "")
  return `${baseUrl}${path.startsWith("/") ? "" : "/"}${path}`
}

function parseExtractResponse(response) {
  const payload = response.data || {}
  if (response.statusCode !== 200 || !payload.success) {
    throw new Error(payload.error || "解析失败")
  }

  const media = payload.data || {}
  media.url = absoluteUrl(media.url)
  media.preview_url = absoluteUrl(media.preview_url)
  media.cover = absoluteUrl(media.cover)
  media.images = (media.images || []).map(absoluteUrl)
  media.live_photos = (media.live_photos || []).map(absoluteUrl)
  return media
}

function callContainer(text, includeLive = true) {
  return wx.cloud.callContainer({
    config: { env: CLOUD_ENV },
    path: "/api/extract",
    method: "POST",
    timeout: 120000,
    header: {
      "X-WX-SERVICE": CLOUD_SERVICE,
      "content-type": "application/json"
    },
    data: { url: text, include_live: includeLive }
  }).then(parseExtractResponse)
}

function requestByHttp(text, includeLive = true) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${API_BASE_URL}/api/extract`,
      method: "POST",
      data: { url: text, include_live: includeLive },
      timeout: 120000,
      success(response) {
        try {
          resolve(parseExtractResponse(response))
        } catch (error) {
          reject(error)
        }
      },
      fail(error) {
        reject(new Error(error.errMsg || "网络请求失败"))
      }
    })
  })
}

function delay(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds))
}

async function extractMedia(text) {
  const isDouyin = /(?:douyin\.com|iesdouyin\.com)/i.test(text)
  if (isDouyin) {
    // Return the image collection immediately. Live Photo detection uses a
    // separate, longer browser lookup so it cannot block normal image posts.
    return requestByHttp(text, false)
  }

  if (!(wx.cloud && CLOUD_ENV && CLOUD_SERVICE)) {
    return requestByHttp(text)
  }

  let firstError
  try {
    return await callContainer(text)
  } catch (error) {
    firstError = error
    console.warn("首次云托管解析失败，准备重试", error)
  }

  await delay(1500)
  try {
    return await callContainer(text)
  } catch (error) {
    console.warn("云托管重试失败，尝试公网接口", error)
  }

  try {
    return await requestByHttp(text)
  } catch (httpError) {
    const message = String(firstError && (firstError.errMsg || firstError.message) || "")
    if (message.includes("102002")) {
      throw new Error("云托管实例启动或解析超时（102002）。请稍后重试；抖音 Live 解析建议云托管使用 2 核 4G，并将最小实例数设为 1。")
    }
    throw httpError
  }
}

function extractDouyinLive(text) {
  // Direct HTTP requests can wait longer than callContainer and avoid its
  // 102002 timeout while Chromium verifies the public Douyin page.
  return requestByHttp(text, true)
}

module.exports = {
  absoluteUrl,
  extractMedia,
  extractDouyinLive
}
