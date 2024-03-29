# -*- coding: utf-8 -*-

# Form implementation generated from reading ui file 'untitled.ui'
#
# Created by: PyQt5 UI code generator 5.15.6
#
# WARNING: Any manual changes made to this file will be lost when pyuic5 is
# run again.  Do not edit this file unless you know what you are doing.


from PyQt5 import QtCore, QtGui, QtWidgets
import getaddr, refresh, mandatx
import subprocess, webbrowser
import txlist, import_pk, export
import requests

class Ui_MainWindow(object):
    def setupUi(self, MainWindow):
        MainWindow.setObjectName("MainWindow")
        MainWindow.setFixedSize(483, 373)
        self.centralwidget = QtWidgets.QWidget(MainWindow)
        self.centralwidget.setObjectName("centralwidget")
        self.pushButton = QtWidgets.QPushButton(self.centralwidget)
        self.pushButton.setGeometry(QtCore.QRect(330, 50, 111, 23))
        self.pushButton.setObjectName("pushButton")
        self.pushButton_2 = QtWidgets.QPushButton(self.centralwidget)
        self.pushButton_2.setGeometry(QtCore.QRect(230, 120, 75, 23))
        self.pushButton_2.setObjectName("pushButton_2")
        self.label = QtWidgets.QLabel(self.centralwidget)
        self.label.setGeometry(QtCore.QRect(30, 30, 111, 16))
        font = QtGui.QFont()
        font.setPointSize(10)
        self.label.setFont(font)
        self.label.setObjectName("label")
        self.comboBox = QtWidgets.QComboBox(self.centralwidget)
        self.comboBox.setGeometry(QtCore.QRect(30, 50, 171, 22))
        self.comboBox.setObjectName("comboBox")
        self.pushButton_3 = QtWidgets.QPushButton(self.centralwidget)
        self.pushButton_3.setGeometry(QtCore.QRect(230, 50, 75, 23))
        self.pushButton_3.setObjectName("pushButton_3")
        self.label_2 = QtWidgets.QLabel(self.centralwidget)
        self.label_2.setGeometry(QtCore.QRect(30, 90, 111, 16))
        font = QtGui.QFont()
        font.setPointSize(10)
        self.label_2.setFont(font)
        self.label_2.setObjectName("label_2")
        self.label_3 = QtWidgets.QLabel(self.centralwidget)
        self.label_3.setGeometry(QtCore.QRect(30, 120, 111, 16))
        font = QtGui.QFont()
        font.setPointSize(14)
        self.label_3.setFont(font)
        self.label_3.setObjectName("label_3")
        self.label_4 = QtWidgets.QLabel(self.centralwidget)
        self.label_4.setGeometry(QtCore.QRect(30, 220, 47, 21))
        font = QtGui.QFont()
        font.setPointSize(10)
        self.label_4.setFont(font)
        self.label_4.setObjectName("label_4")
        self.lineEdit = QtWidgets.QLineEdit(self.centralwidget)
        self.lineEdit.setGeometry(QtCore.QRect(30, 250, 113, 20))
        self.lineEdit.setObjectName("lineEdit")
        self.label_5 = QtWidgets.QLabel(self.centralwidget)
        self.label_5.setGeometry(QtCore.QRect(170, 220, 16, 21))
        font = QtGui.QFont()
        font.setPointSize(10)
        self.label_5.setFont(font)
        self.label_5.setObjectName("label_5")
        self.lineEdit_2 = QtWidgets.QLineEdit(self.centralwidget)
        self.lineEdit_2.setGeometry(QtCore.QRect(170, 250, 113, 20))
        self.lineEdit_2.setObjectName("lineEdit_2")
        self.pushButton_4 = QtWidgets.QPushButton(self.centralwidget)
        self.pushButton_4.setGeometry(QtCore.QRect(330, 250, 75, 23))
        self.pushButton_4.setObjectName("pushButton_4")
        self.pushButton_5 = QtWidgets.QPushButton(self.centralwidget)
        self.pushButton_5.setGeometry(QtCore.QRect(330, 280, 75, 23))
        self.pushButton_5.setObjectName("pushButton_5")
        self.pushButton_6 = QtWidgets.QPushButton(self.centralwidget)
        self.pushButton_6.setGeometry(QtCore.QRect(330, 310, 111, 23))
        self.pushButton_6.setObjectName("pushButton_6")
        self.label_ok = QtWidgets.QLabel(self.centralwidget)
        self.label_ok.setGeometry(QtCore.QRect(30, 290, 300, 41))
        font = QtGui.QFont()
        font.setPointSize(10)
        self.label_ok.setFont(font)
        self.label_ok.setText("")
        self.label_ok.setObjectName("label_ok")
        self.pushButton_7 = QtWidgets.QPushButton(self.centralwidget)
        self.pushButton_7.setGeometry(QtCore.QRect(330, 120, 121, 21))
        self.pushButton_7.setObjectName("pushButton_7")
        self.pushButton_8 = QtWidgets.QPushButton(self.centralwidget)
        self.pushButton_8.setGeometry(QtCore.QRect(150, 180, 75, 23))
        self.pushButton_8.setObjectName("pushButton_8")
        self.pushButton_9 = QtWidgets.QPushButton(self.centralwidget)
        self.pushButton_9.setGeometry(QtCore.QRect(260, 180, 75, 23))
        self.pushButton_9.setObjectName("pushButton_9")
        MainWindow.setCentralWidget(self.centralwidget)
        self.menubar = QtWidgets.QMenuBar(MainWindow)
        self.menubar.setGeometry(QtCore.QRect(0, 0, 483, 21))
        self.menubar.setObjectName("menubar")
        MainWindow.setMenuBar(self.menubar)
        self.statusbar = QtWidgets.QStatusBar(MainWindow)
        self.statusbar.setObjectName("statusbar")
        MainWindow.setStatusBar(self.statusbar)

        self.retranslateUi(MainWindow)
        QtCore.QMetaObject.connectSlotsByName(MainWindow)

    def retranslateUi(self, MainWindow):
        _translate = QtCore.QCoreApplication.translate
        MainWindow.setWindowTitle(_translate("ScarletCoin Wallet", "ScarletCoin Wallet"))

        #pushButton = get new address
        #pushButton2 = refresh
        #pushButton3= copia
        #pushButton4= send
        #5 = copy txid
        #6 = lookup on explorer
        self.pushButton.setText(_translate("MainWindow", "Get new address"))
        self.pushButton_2.setText(_translate("MainWindow", "Refresh"))
        self.label.setText(_translate("MainWindow", "Your address(es):"))
        self.pushButton_3.setText(_translate("MainWindow", "Copy"))
        self.label_2.setText(_translate("MainWindow", "Your ScarletCoins"))
        self.label_3.setText(_translate("MainWindow", "1234567890"))
        self.label_4.setText(_translate("MainWindow", "Send"))
        self.lineEdit.setText(_translate("MainWindow", "12345"))
        self.label_5.setText(_translate("MainWindow", "to"))
        self.lineEdit_2.setText(_translate("MainWindow", "abcdef123"))
        self.pushButton_4.setText(_translate("MainWindow", "Send"))
        self.label_ok.setText(_translate("MainWindow", ""))
        self.pushButton_5.setText(_translate("MainWindow", "Copy txid"))
        self.pushButton_6.setText(_translate("MainWindow", "Lookup on explorer"))
        self.pushButton_7.setText(_translate("MainWindow", "Open transactions list"))
        self.pushButton_8.setText(_translate("MainWindow", "Import"))
        self.pushButton_9.setText(_translate("MainWindow", "Export"))
        self.pushButton.clicked.connect(self.ga)
        self.pushButton_2.clicked.connect(self.ref)
        self.pushButton_3.clicked.connect(self.copia)
        self.pushButton_4.clicked.connect(self.send)
        self.pushButton_5.clicked.connect(self.copiatxid)
        self.pushButton_6.clicked.connect(self.apriweb)
        self.pushButton_7.clicked.connect(self.apritxlist)
        self.pushButton_8.clicked.connect(self.importprivatekey)
        self.pushButton_9.clicked.connect(self.exportprivatekey)


        with open('data/addresslist.txt', 'r') as f:
            addresses=f.readlines()

        try:
            if addresses[0]=='\n':
                self.ga()
        except:
            if addresses==[]:
                self.ga()

        for l in addresses:
            if l!='\n':
                appendi=l[:16]
                self.comboBox.addItem(appendi)
        
        #print(self.comboBox.currentText())

        actualaddress=self.comboBox.currentText()
        actualcoins=refresh.ref(actualaddress)
        self.label_3.setText(str(actualcoins))
        self.comboBox.currentTextChanged.connect(self.on_combobox_changed)


    def on_combobox_changed(self):
        actualaddress=self.comboBox.currentText()
        actualcoins=refresh.ref(actualaddress)
        if actualcoins=='Address does not exist':
            font = QtGui.QFont()
            font.setPointSize(8)
            self.label_3.setFont(font)
            self.label_3.setText(actualcoins)
        else:
            font = QtGui.QFont()
            font.setPointSize(14)
            self.label_3.setFont(font)
            self.label_3.setText(str(actualcoins))
    def copia(self):
        actualaddress=self.comboBox.currentText()
        cmd='echo '+actualaddress+'|clip'
        self.label_ok.setText('Address copied to clipboard')
        return subprocess.check_call(cmd, shell=True)

    def ga(self):
        address=getaddr.askaddr()
        if len(address)==16:
            self.comboBox.addItem(address)
        else:
            self.label_ok.setText('Generic error. Try again')
    
    def ref(self):
        actualaddress=self.comboBox.currentText()
        actualcoins=refresh.ref(actualaddress)
        self.label_3.setText(str(actualcoins))

    def send(self):

        amount=self.lineEdit.text().strip()
        dest=self.lineEdit_2.text().strip()

        with open('data/addresslist.txt','r') as f:
            addresses=f.read()
        actualaddress=self.comboBox.currentText()
        pos=addresses.find(actualaddress)
        
        completeaddress=addresses[pos:pos+81]
        print('sending '+amount+' to '+dest+' from '+actualaddress)
        #dì done o error
        ris=mandatx.manda(amount,completeaddress,dest)
        self.label_ok.setText(ris)

        #txid + explorer
        if len(ris)==46: #14 lettere + 32 di txid
            global txid 
            global webtx
            txid=ris[14:]
            with open('data/explorer.txt','r') as f:
                explorer=f.read()
            webtx=explorer+txid+'.html'

        actualaddress=self.comboBox.currentText()
        actualcoins=refresh.ref(actualaddress)
        self.label_3.setText(str(actualcoins))
        
    def copiatxid(self):
        try:
            cmd='echo '+txid+'|clip'
            self.label_ok.setText('TXID copied to clipboard')
            return subprocess.check_call(cmd, shell=True)
        except:
            self.label_ok.setText('Make a transaction first')
    def apriweb(self):
        try:
            webbrowser.open(webtx)
        except:
            self.label_ok.setText('Make a transaction first')

    def apritxlist(self):
        actualaddress=self.comboBox.currentText()
        self.window = QtWidgets.QMainWindow()
        self.ui = txlist.Ui_txlist()
        self.ui.setupUi(self.window,actualaddress)
        self.window.show()

    def importprivatekey(self):
        self.window = QtWidgets.QMainWindow()
        self.ui = import_pk.Ui_MainWindow()
        self.ui.setupUi(self.window)
        self.window.show()

        self.ui.pushButton.clicked.connect(self.updateWindow)

    def updateWindow(self):
        with open('data/server.txt', 'r') as f:
            url=f.read()
        with open('data/addresslist.txt', 'r') as f:
            addresses=f.readlines()
        hash=self.ui.lineEdit.text()
        hash=hash.strip()
        for l in addresses:
            l=l.strip()
            if hash==l[17:]:
                self.ui.label_2.setText("Address already exists")
                return('address already exists')

        request='import'+hash
        response=requests.post(url, request)
        response=response.text
        if response=='non existant':
            self.ui.label_2.setText("Wrong private key. Try again")
        else:
            self.ui.label_2.setText("Imported successfully")
            print('yea!!!!')
            with open('data/addresslist.txt', 'a') as f:
                f.write(response+'\n')
            self.comboBox.addItem(response[:16])

    def exportprivatekey(self):
        actualaddress=self.comboBox.currentText()
        self.window = QtWidgets.QMainWindow()
        self.ui = export.Ui_MainWindow()
        self.ui.setupUi(self.window,actualaddress)
        self.window.show()

if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication(sys.argv)
    MainWindow = QtWidgets.QMainWindow()
    ui = Ui_MainWindow()
    ui.setupUi(MainWindow)
    MainWindow.show()
    sys.exit(app.exec_())
