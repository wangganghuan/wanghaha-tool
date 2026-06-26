const { CLOUD_ENV } = require("./config")

App({
  onLaunch() {
    if (wx.cloud && CLOUD_ENV) {
      wx.cloud.init({
        env: CLOUD_ENV,
        traceUser: true
      })
    }
  }
})
