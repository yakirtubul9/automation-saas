# automation-saas

git add .

git commit -m "Short message"

git push

https://automation-saas-1.onrender.com/

https://dashboard.render.com/

# **סדר עבודה מהאייפון (הכי פרקטי)**

פותח Codespace / סביבת פיתוח בענן לריפו שלך (מהאייפון).

עורך קבצים (VS Code Web/Codespace). 

בטרמינל בתוך ה-Codespace:

git status

git add .

git commit -m "עדכון קבצים"

git push

Render עושה Deploy אוטומטי מה-GitHub (אם Auto-Deploy מופעל).

בודק מהטלפון את ה-URL הציבורי: https://automation-saas-1.onrender.com/

אם משהו לא עובד: פותח Logs ב-Render ורואה שגיאה.

# **בקצרה: Render, Django, Git — מי עושה מה?**

Django: הקוד של האתר/המערכת שלך (שרת, דפים, לוגין, DB, לוגיקה).

Git + GitHub: “כפתור השמירה והגרסאות”. כל שינוי בקוד נשמר כ-commit ועולה לריפו.

Render: השרת שמריץ באינטרנט את מה שיש ב-GitHub. הוא מושך את הקוד מה-repo, מתקין requirements, מריץ migrations, ומפעיל את האתר על דומיין onrender.com.