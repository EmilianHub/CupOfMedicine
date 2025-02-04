import hashlib
import random
import re
from datetime import datetime, timedelta

from flask import jsonify
from sqlalchemy import select, update, func, Interval

import restartCodeCache as restartCodeCache
import rsaEncryption
from diseaseCache import clearCache
from localizationJPA import Localization
from locationService import getCurrentLocation
from chorobyJPA import Diseases
from dbConnection import db_session
from emailService import EmailService
import jwtService
from userDiseaseHistoryJPA import UserDiseaseHistory
from userJPA import User

emailService = EmailService()
passwordRegex = re.compile("^(?=.*[0-9!@#$%^&+=])(?=.*[a-z])(?=.*[A-Z])(?=\\S+$).{8,}$")
TOKEN_EXPIRATION_OFFSET = 10


class UserService:
    # That makes the class Singleton
    __instance = None

    def __new__(cls, *args, **kwargs):
        if cls.__instance is None:
            cls.__instance = super(UserService, cls).__new__(cls)
        return cls.__instance

    def sendRestartCodeToEmail(self, email: str):
        code = random.randint(1000, 9999)
        isExist = self.__isUserExist(email)
        if isExist:
            isSent = emailService.sendEmailWithRestartCode(email, code)
            if isSent:
                restartCodeCache.add(email, code)
                return "Message has been send to given email", 200

            return "Something gone wrong, message has not been sent", 400

        return "User with given email doesn't exist", 404

    def __isUserExist(self, email: str):
        try:
            result = self.findUserWithEmail(email)
            return result is not None
        except Exception as error:
            print("Error occurred while looking for user: ", error)

        return False

    def verifyRestartCode(self, email: str, code: int):
        tempCache = restartCodeCache.getWithCode(code)
        if tempCache is not None and tempCache.keys().__contains__(email):
            return "Correct", 200
        return "Incorrect", 400

    def updatePassword(self, email: str, password: str):
        if passwordRegex.match(str(password)):
            try:
                d = hashlib.sha256(password.encode())
                encodePassword = d.hexdigest()
                query = update(User).where(User.email == email).values(password=encodePassword)
                result = db_session.execute(query)
                db_session.commit()
                if result.rowcount != 0:
                    return "Password updated", 200
            except Exception as error:
                print("Error occurred while updating user: ", error)

            return "Something gone wrong. Password has not been updated", 400

        return "Password should contain at least one uppercase and one special character", 400

    def register(self, email: str, password: str):
        try:
            d = hashlib.sha256(password.encode())
            hash = d.hexdigest()

            newUser = User(email=email, password=hash)
            db_session.add(newUser)
            db_session.commit()
            return "Zarejestrowano", 200

        except Exception as error:
            print(error)

        return 'Niepoprawne dane rejestracyjne', 401

    def tryLogin(self, email: str, password: str):
        user = self.findUserWithEmail(email)
        if user is not None and self.__isPasswordCorrect(password, user):
            token = jwtService.generateToken(user.email)
            clearCache()
            return jsonify({'token': token}), 200
        else:
            return jsonify({'error': 'Nieprawidłowe dane logowania'}), 401

    def __isPasswordCorrect(self, plainPassword, user):
        return hashlib.sha256(plainPassword.encode('utf-8')).hexdigest() == user.password

    def findUserWithEmail(self, email):
        return User.query.filter_by(email=email).first()

    def verifyAuthentication(self, token):
        data = jwtService.decodeRequest(token)
        result = self.findUserWithEmail(data.get("email"))
        if result is None:
            return False
        return True

    def saveDiseaseHistory(self, userSymptoms: [], disease: str, token, confidence):
        try:
            symptoms = ""
            for msg in userSymptoms:
                symptoms += msg + ",\n "

            encryptedSymptoms = rsaEncryption.encrypt(symptoms)
            diseaseJPA = self.findDiseaseReferance(disease)
            userJPA = self.findUserWithEmail(token.get("email"))
            self.mergeHistory(encryptedSymptoms, diseaseJPA, userJPA, token, confidence)

            return "History saved", 200

        except(Exception) as error:
            print("Error while saving user history: ", error)

        return "Something gone wrong", 400

    def findDiseaseReferance(self, disease: str):
        query = select(Diseases).where(Diseases.choroba == disease)
        return db_session.scalars(query).one()

    def mergeHistory(self, symtoms, diseaseJPA, userJPA, token, confidence):
        exp = datetime.utcfromtimestamp(token.get("exp"))
        query = select(UserDiseaseHistory) \
            .where(UserDiseaseHistory.user_id == userJPA.id) \
            .where(UserDiseaseHistory.created + func.cast(timedelta(minutes=TOKEN_EXPIRATION_OFFSET), Interval) >= exp)
        historyJPA = db_session.scalars(query).one_or_none()

        newHistoryJPA = UserDiseaseHistory(user=userJPA, user_symptoms=symtoms, disease=diseaseJPA,
                                           confidence=confidence, created=datetime.now())
        if historyJPA is not None:
            newHistoryJPA.id = historyJPA.id
            newHistoryJPA.created = historyJPA.created

        db_session.merge(newHistoryJPA)
        db_session.commit()

    def saveRegionDisease(self, latitude: str, longitude: str, disease: str):
        try:
            location = getCurrentLocation(longitude, latitude)
            diseaseJPA = self.findDiseaseReferance(disease)
            city = self.__findCityOrVillage(location["address"])
            token = jwtService.decodeAuthorizationHeaderToken()
            if token:
                date = token.get("exp")
                token = datetime.utcfromtimestamp(date)
            else:
                token = jwtService.getSessionToken()

            self.mergeRegionHistory(location, city, diseaseJPA, token)

            return "Disease localization saved"
        except Exception as error:
            print(error)

        return "Disease localization not saved"

    def mergeRegionHistory(self, location, city, diseaseJPA, token):
        query = select(Localization).where(Localization.session_token == token)
        locationJPA = db_session.scalars(query).one_or_none()

        newLocalizationJPA = Localization(woj=location["address"]["state"], miasto=city, choroba=diseaseJPA,
                                          session_token=token, created=datetime.now())

        if locationJPA:
            newLocalizationJPA.id_loc = locationJPA.id_loc
            newLocalizationJPA.created = locationJPA.created

        db_session.merge(newLocalizationJPA)
        db_session.commit()

    def __findCityOrVillage(self, address):
        for k, v in address.items():
            if k == "city" or k == "village":
                return v

    def editEmail(self, email: str, newEmail: str):
        try:
            query = update(User).where(User.email == email).values(email=newEmail)
            result = db_session.execute(query)
            db_session.commit()

            if result.rowcount != 0:
                return "Email updated", 200
            return "Something gone wrong. email has not been updated", 400
        except Exception as error:
            print("Error occurred while updating user: ", error)

        return "Something gone wrong. email has not been updated", 400

    def findUserHistory(self, email):
        try:
            query = select(UserDiseaseHistory).join(UserDiseaseHistory.user).where(User.email.ilike(email))
            result = db_session.scalars(query).fetchall()
            if result:
                return [{
                    "Objawy": " ".join(rsaEncryption.decrypt(r.user_symptoms).split(",")),
                    "Choroba": r.disease.choroba,
                    "id": r.id,
                    "created": r.created
                } for r in result]
            return []
        except Exception as error:
            print("Error occurred while updating user: ", error)

        return []
