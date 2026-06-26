const { extractMedia, extractDouyinLive } = require("../../utils/api")

function buildImageItems(media) {
  return (media.images || []).map((url, index) => ({
    url,
    poster: url,
    selected: true,
    filename: `image_${index + 1}.jpg`
  }))
}

function buildLiveItems(media) {
  return (media.live_photos || []).map((url, index) => ({
    url,
    poster: (media.images || [])[index] || media.cover || "",
    selected: Boolean(url),
    filename: `live_${index + 1}.mp4`
  })).filter(item => item.url)
}

Page({
  data: {
    inputText: "",
    loading: false,
    media: null,
    activeMediaTab: "images",
    liveStatus: "",
    imageItems: [],
    liveItems: [],
    items: [],
    selectedCount: 0
  },

  onInput(event) {
    this.setData({ inputText: event.detail.value })
  },

  paste() {
    wx.getClipboardData({
      success: ({ data }) => this.setData({ inputText: data || "" })
    })
  },

  async extract() {
    const text = this.data.inputText.trim()
    if (!text || this.data.loading) {
      if (!text) wx.showToast({ title: "请先粘贴链接", icon: "none" })
      return
    }

    this.setData({
      loading: true,
      media: null,
      activeMediaTab: "images",
      liveStatus: "",
      imageItems: [],
      liveItems: [],
      items: [],
      selectedCount: 0
    })

    try {
      const media = await extractMedia(text)
      const imageItems = buildImageItems(media)
      const liveItems = buildLiveItems(media)
      this.setData({
        media,
        imageItems,
        liveItems,
        items: imageItems,
        selectedCount: imageItems.length
      })
      if (media.platform === "抖音" && media.media_type === "images") {
        this.loadDouyinLive(text)
      }
    } catch (error) {
      wx.showModal({
        title: "解析失败",
        content: error.message || error.errMsg || "请稍后重试",
        showCancel: false
      })
    } finally {
      this.setData({ loading: false })
    }
  },

  async loadDouyinLive(text) {
    if (this.data.liveStatus === "loading") return
    this.setData({ liveStatus: "loading" })
    try {
      const enrichedMedia = await extractDouyinLive(text)
      const liveItems = buildLiveItems(enrichedMedia)
      const updates = {
        media: { ...this.data.media, live_photos: enrichedMedia.live_photos || [] },
        liveItems,
        liveStatus: liveItems.length ? "ready" : "none"
      }
      if (this.data.activeMediaTab === "live") {
        updates.items = liveItems
        updates.selectedCount = liveItems.length
      }
      this.setData(updates)
      if (liveItems.length) {
        wx.showToast({ title: `识别到 ${liveItems.length} 个 Live`, icon: "none" })
      }
    } catch (error) {
      console.error("抖音 Live 识别失败", error)
      this.setData({ liveStatus: "error" })
    }
  },

  retryDouyinLive() {
    const text = this.data.inputText.trim()
    if (text) this.loadDouyinLive(text)
  },

  switchMediaTab(event) {
    const tab = event.currentTarget.dataset.tab
    const items = tab === "live" ? this.data.liveItems : this.data.imageItems
    this.setData({
      activeMediaTab: tab,
      items,
      selectedCount: items.filter(item => item.selected).length
    })
  },

  updateActiveItems(items) {
    const key = this.data.activeMediaTab === "live" ? "liveItems" : "imageItems"
    this.setData({
      [key]: items,
      items,
      selectedCount: items.filter(item => item.selected).length
    })
  },

  toggleItem(event) {
    const index = Number(event.currentTarget.dataset.index)
    const items = this.data.items.map((item, itemIndex) => (
      itemIndex === index ? { ...item, selected: !item.selected } : item
    ))
    this.updateActiveItems(items)
  },

  selectAll() {
    this.updateActiveItems(this.data.items.map(item => ({ ...item, selected: true })))
  },

  clearSelection() {
    this.updateActiveItems(this.data.items.map(item => ({ ...item, selected: false })))
  },

  saveMedia() {
    const { media, activeMediaTab, items } = this.data
    if (!media) return

    if (media.media_type === "video") {
      this.downloadAndSave(media.preview_url || media.url, "video")
      return
    }

    const selected = items.filter(item => item.selected)
    if (!selected.length) {
      wx.showToast({
        title: activeMediaTab === "live" ? "请选择 Live 实况" : "请选择图片",
        icon: "none"
      })
      return
    }
    this.saveItemsSequentially(selected, 0, activeMediaTab === "live" ? "video" : "image")
  },

  saveItemsSequentially(items, index, type) {
    if (index >= items.length) {
      wx.hideLoading()
      wx.showToast({ title: "保存完成", icon: "success" })
      return
    }

    wx.showLoading({ title: `${index + 1}/${items.length}` })
    this.downloadAndSave(items[index].url, type, () => {
      this.saveItemsSequentially(items, index + 1, type)
    }, true)
  },

  downloadAndSave(url, type, done, quiet = false) {
    if (!quiet) wx.showLoading({ title: "下载中" })
    wx.downloadFile({
      url,
      timeout: 180000,
      success: ({ statusCode, tempFilePath }) => {
        if (statusCode !== 200 && statusCode !== 206) {
          this.handleSaveError(new Error(`下载失败：${statusCode}`))
          return
        }

        const save = type === "video"
          ? wx.saveVideoToPhotosAlbum
          : wx.saveImageToPhotosAlbum
        save({
          filePath: tempFilePath,
          success: () => {
            if (!quiet) {
              wx.hideLoading()
              wx.showToast({ title: "保存成功", icon: "success" })
            }
            if (done) done()
          },
          fail: error => this.handleSaveError(error)
        })
      },
      fail: error => this.handleSaveError(error)
    })
  },

  handleSaveError(error) {
    wx.hideLoading()
    const message = error.errMsg || error.message || "请稍后重试"
    const denied = String(message).includes("auth deny")
    wx.showModal({
      title: "保存失败",
      content: denied ? "请在设置中允许访问相册。" : message,
      confirmText: denied ? "去设置" : "知道了",
      success: result => {
        if (denied && result.confirm) wx.openSetting()
      }
    })
  },

  onVideoError(event) {
    console.error("视频播放失败", event.detail || {})
    wx.showToast({
      title: "预览生成中，请稍后重试播放",
      icon: "none",
      duration: 3000
    })
  },

  copyLink() {
    const { media, items } = this.data
    if (!media) return
    const text = media.media_type === "video"
      ? media.url
      : items.filter(item => item.selected).map(item => item.url).join("\n")
    wx.setClipboardData({ data: text || "" })
  }
})
