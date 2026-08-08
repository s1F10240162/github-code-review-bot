import subprocess


def get_user(db_conn, username):
    # ユーザー入力をそのままSQLに埋め込んでいる (SQLインジェクション)
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    return db_conn.execute(query)


def run_command(user_input):
    # shell=Trueでユーザー入力をそのまま実行している (コマンドインジェクション)
    return subprocess.run(user_input, shell=True)


def add_item(item, items=[]):
    # ミュータブルなデフォルト引数。呼び出し間で状態が共有されてしまう
    items.append(item)
    return items


def divide(a, b):
    # ゼロ除算が考慮されていない
    return a / b


def read_config(path):
    # ファイルをcloseしておらずリソースリークの可能性がある
    f = open(path, "r")
    data = f.read()
    return data


def calculate_total(prices):
    l = 0
    for p in prices:
        l = l + p
    return l
