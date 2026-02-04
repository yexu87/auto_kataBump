# auto_kataBump  登录续期


你需要做以下修改：


## 1、设置环境变量，环境变量格式如下：
```
KATABUMP_BATCH='a1@example.com,pass1,218445,123456:AAxxxxxx,123456789
a2@example.com,pass2,998877,123456:AAyyyyyy,-10022223333
a3@example.com,pass3,556677'
```

### 格式为:
 email,password,server_id,tg_bot_token,tg_chat_id


每行一套数据：

1、不发 TG：email,password,server_id

2、发 TG：email,password,server_id,tg_bot_token,tg_chat_id

注意:server_id为续期界面中的url里面的id编号，每个人的id都会不一样



## 2、开放自动写time.txt的文件权限。
### 这个主要是用于规避github 默认60天的仓库代码没有任何变动就会自动给你发邮件并且自动禁用action的定时任务。

GITHUB_TOKEN 不用你手动创建 —— 只要你的 workflow 在 GitHub Actions 里跑起来，GitHub 会自动为每次运行生成一个临时的 secrets.GITHUB_TOKEN，在 workflow 里直接用就行（你已经在用 ${{ secrets.GITHUB_TOKEN }} 了）。

### 你需要做的通常是 给它权限、以及确认 checkout 会把它用在 push 上。

1) 你不需要创建：它默认就存在,在 workflow 里这样写就能用：
```
env:
  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

```

这个 secrets.GITHUB_TOKEN 是 GitHub 自动注入的，不会出现在 “Secrets” 列表里让你手动建。


2) 你需要设置的地方：给它写权限

到仓库：

### Settings → Actions → General → Workflow permissions

选择：

✅ Read and write permissions

（如果是 “Read repository contents permission” 只读，那 git push 会失败。）

你 workflow 里这段也要保留（你已经写对了）：

```
permissions:
  contents: write
```

## 3、修改定时任务执行时间。
### main.yml里面修改你的定时任务的执行时间（建议每天执行一次，你需要修改的是他的小时和分钟，一定要每天执行，因为代码里面是每天检查是否到了续期日，只有到续期日的前一天才会进行续期操作。）


