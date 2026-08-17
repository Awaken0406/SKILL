import os
import subprocess
from pathlib import Path
import time
from subprocess import TimeoutExpired, CalledProcessError
def git_commit_push(files_to_add="."):
    repo_path = "./"
    """
    执行git add/commit/push操作
    :param repo_path: 仓库路径（绝对路径）
    :param files_to_add: 要添加的文件（默认全部）
    """
    try:
        # 切换到仓库目录
        original_dir = Path.cwd()
        os.chdir(repo_path)

        add_process = subprocess.run(["git", "pull"], 
                                     check=True,
                                     capture_output=True,
                                     text=True)
        print("✔️ Git pull 成功")

        # 执行git add
        add_process = subprocess.run(["git", "add", files_to_add], 
                                     check=True,
                                     capture_output=True,
                                     text=True)
        print("✔️ Git add 成功")

        # 执行git commit
        commit_process = subprocess.run(["git", "commit", "-m", "Auto commit from Python script"],
                                        check=True,
                                        capture_output=True,
                                        text=True)
        print(f"✔️ Git commit 成功")


        max_retries = 3  # 最大重试次数
        timeout = 15      # 单次超时时间

        for attempt in range(max_retries):
            try:
                # 执行git push
                push_process = subprocess.run(["git", "push"],
                                       check=True,
                                       capture_output=True,
                                       text=True,
                                       timeout=timeout)
                print("✔️ Git push 成功")
                break
            except TimeoutExpired as e:
                print(f"第 {attempt+1} 次尝试超时")
                if attempt == max_retries - 1:
                    print("超过最大重试次数，放弃推送")
                    raise
                time.sleep((attempt + 1))  
            except CalledProcessError as e:
                print(f"非网络错误（代码 {e.returncode}）:\n{e.stderr}")
                

        # 返回原目录
        os.chdir(original_dir)
        return True
    
    except subprocess.CalledProcessError as e:
        print(f"❌ Git操作失败：{e.stderr}")
        return False
    except Exception as e:
        print(f"❌ 发生意外错误：{str(e)}")
        return False

if __name__ == "__main__": 
    git_commit_push()
